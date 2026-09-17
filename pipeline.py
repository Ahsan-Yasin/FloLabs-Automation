import json
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.models import JobRecord, JobStatus
from decide import build_segments, get_decisions
from edl import build_edl
from ingest.validate import AVSyncError, validate_video
from slice.pipeline import render_output
from slice.transcript import remap_transcript
from transcribe import flag_overlaps, transcribe

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

    try:
        job.status = JobStatus.TRANSCRIBING
        update(job)
        words = transcribe(source_path)
        words = flag_overlaps(words)
        _write_json(job_dir / "transcript.json", [w.model_dump() for w in words])
        job.transcript_path = str(job_dir / "transcript.json")

        job.status = JobStatus.DECIDING
        update(job)
        segments = build_segments(words)
        decisions = get_decisions(segments)

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
