import json
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.models import JobRecord, JobStatus
from decide import build_segments, get_decisions
from edl import build_edl
from ingest.validate import AVSyncError, validate_video
from ingest.youtube import download_youtube
from slice.pipeline import render_output
from slice.transcript import remap_transcript
from transcribe import (
    fetch_youtube_transcript,
    flag_overlaps,
    load_uploaded_transcript,
    transcribe,
)

logger = get_logger(__name__)


def run_pipeline(job: JobRecord, update: "callable[[JobRecord], None]") -> None:
    """End-to-end pipeline (section 2): transcribe -> overlap flags -> LLM decide
    -> EDL -> slice -> concat -> clean transcript. Mutates `job` and calls
    `update` after each stage so callers can persist/observe progress.
    """
    settings = get_settings()
    source_path = Path(job.source_path)
    job_dir = settings.jobs_dir / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        video_info = validate_video(source_path)
    except AVSyncError as exc:
        logger.warning("job %s: %s", job.job_id, exc)
        job.status = JobStatus.SKIPPED_DESYNC
        job.error = str(exc)
        update(job)
        return
    except Exception as exc:
        # ffprobe itself can fail in ways that aren't AVSyncError (corrupt/
        # truncated upload, missing ffprobe binary, a hung process past
        # ffprobe_timeout_seconds, unparsable output). Without this, those
        # exceptions escaped run_pipeline entirely and left the job stuck in
        # QUEUED forever since job.status was never updated.
        logger.exception("job %s: pre-flight validation failed", job.job_id)
        job.status = JobStatus.FAILED
        job.error = str(exc)
        update(job)
        return

    try:
        job.status = JobStatus.TRANSCRIBING
        update(job)

        # Reuse a platform-provided transcript when we have one (a user-supplied
        # export for uploads, or YouTube's own captions) — skips the WhisperX
        # ASR/diarization pass entirely. Falls back to self-hosted transcription
        # whenever no transcript is available or it fails to parse.
        native = None
        if job.native_transcript_path:
            native = load_uploaded_transcript(Path(job.native_transcript_path))
            if native is not None:
                job.transcript_source = "uploaded_transcript"
        if native is None and job.source_url:
            native = fetch_youtube_transcript(job.source_url)
            if native is not None:
                job.transcript_source = "youtube_captions"

        if native is not None:
            words, segments = native
            words = flag_overlaps(words)
        else:
            job.transcript_source = "asr"
            words = transcribe(source_path)
            words = flag_overlaps(words)
            segments = build_segments(words)

        _write_json(job_dir / "transcript.json", [w.model_dump() for w in words])
        job.transcript_path = str(job_dir / "transcript.json")

        job.status = JobStatus.DECIDING
        update(job)
        decisions = get_decisions(segments, mode=job.mode)

        job.status = JobStatus.BUILDING_EDL
        update(job)
        edl = build_edl(decisions, words, video_info.duration)
        _write_json(job_dir / "edl.json", edl.model_dump())
        job.edl_path = str(job_dir / "edl.json")

        job.status = JobStatus.SLICING
        update(job)
        out_path = job_dir / f"output{source_path.suffix}"
        render_output(source_path, edl, out_path, job_dir / "tmp")
        job.output_video_path = str(out_path)

        clean_words = remap_transcript(words, edl)
        _write_json(job_dir / "clean_transcript.json", [w.model_dump() for w in clean_words])
        job.clean_transcript_path = str(job_dir / "clean_transcript.json")

        job.status = JobStatus.DONE
        update(job)
    except Exception as exc:
        logger.exception("job %s failed", job.job_id)
        job.status = JobStatus.FAILED
        job.error = str(exc)
        update(job)


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def run_youtube_pipeline(job: JobRecord, update: "callable[[JobRecord], None]", url: str) -> None:
    """Download a YouTube URL first, then run the normal pipeline against the local file."""
    job.status = JobStatus.DOWNLOADING
    update(job)
    try:
        _, path = download_youtube(url)
    except Exception as exc:  # noqa: BLE001
        # download_youtube wraps most failures as YoutubeDownloadError already,
        # but anything it doesn't catch (e.g. yt_dlp itself missing) must still
        # mark the job FAILED rather than escape and leave it stuck DOWNLOADING.
        logger.warning("job %s: youtube download failed: %s", job.job_id, exc)
        job.status = JobStatus.FAILED
        job.error = str(exc)
        update(job)
        return

    job.source_path = str(path)
    update(job)
    run_pipeline(job, update)
