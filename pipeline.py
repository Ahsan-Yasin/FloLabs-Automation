import hashlib
import json
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from core.config import get_settings
from core.errors import InsufficientDisk, classify
from core.logging import get_logger
from core.models import (
    Decision,
    JobRecord,
    JobStatus,
    Moment,
    RemovedRange,
    Segment,
    Word,
)
from core.timeline import fade_frames_for
from decide import build_segments
from decide.chapters import generate_chapters
from decide.gemini_client import LazyCaller, judge_segments, rerank_moments
from decide.prompts import REMOVAL_LABELS, rerank_prompt
from decide.repair import repair_fragments
from deliver import DeliverInputs, deliver
from edl import build_edl, complement
from edl.highlights import (
    calibrate_by_rank,
    candidate_moments,
    select_highlights,
    select_shorts,
)
from ingest.store import title_from_filename
from ingest.validate import AVSyncError, validate_video
from ingest.youtube import download_youtube
from outputs import RenderInputs
from slice.profile import probe_media
from transcribe import (
    fetch_youtube_transcript,
    flag_overlaps,
    load_uploaded_transcript,
    segments_to_words,
    transcribe,
)

logger = get_logger(__name__)

HIGHLIGHT_MIN_GAP_S = 2.0


def check_disk(min_free: int | None = None) -> None:
    settings = get_settings()
    min_free = settings.min_free_disk_bytes if min_free is None else min_free
    if min_free <= 0:
        return
    free = shutil.disk_usage(settings.storage_dir).free
    if free < min_free:
        raise InsufficientDisk(
            f"only {free / 1024**3:.1f} GiB free under {settings.storage_dir}, need {min_free / 1024**3:.1f} GiB"
        )


def run_pipeline(job: JobRecord, update: "callable[[JobRecord], None]") -> None:
    """End-to-end pipeline: transcribe -> one AI pass (keep/remove + highlight
    score per sentence) -> re-rank the best moments -> EDL on the source frame
    grid + highlights/shorts selection -> [stop here if decide_only] ->
    deliver: final.mp4 (reel + card + cleaned meeting), removed.mp4, shorts,
    transcripts, chapters, report.pdf, manifest.json, bundle.zip.

    Mutates `job` and calls `update` after each stage so callers can
    persist/observe progress (and, under the job queue, cancel). Decisions,
    the re-rank and the transcript are saved as they are produced, so running
    the same job again (e.g. rendering a decide_only job) reuses them.
    """
    settings = get_settings()
    opts = job.options
    source_path = Path(job.source_path)
    job_dir = settings.jobs_dir / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job.warnings = []
    job.error = job.error_code = None
    job.retryable = False
    job.retry_after_s = None
    # a re-render must not serve the previous run's outputs if this one fails
    job.artifacts = {}
    job.bundle_path = job.bundle_sha256 = None
    job.bundle_bytes = None
    job.chapters_path = job.output_video_path = None
    job.final_offset_s = 0.0

    try:
        check_disk()
        video_info = validate_video(source_path)
        media = probe_media(source_path)
    except AVSyncError as exc:
        logger.warning("job %s: %s", job.job_id, exc)
        _fail(job, update, exc, status=JobStatus.SKIPPED_DESYNC, code="source_unsupported")
        return
    except Exception as exc:
        # ffprobe itself can fail in ways that aren't AVSyncError (corrupt/
        # truncated upload, missing ffprobe binary, a hung process past
        # ffprobe_timeout_seconds, unparsable output). Without this, those
        # exceptions escaped run_pipeline entirely and left the job stuck in
        # QUEUED forever since job.status was never updated.
        logger.exception("job %s: pre-flight validation failed", job.job_id)
        code, _, _ = classify(exc)
        _fail(job, update, exc, code="source_unsupported" if code == "internal" else None)
        return

    caller = LazyCaller()
    prior_usage = dict(job.llm_usage)  # a rendered decide_only job keeps its decide-run usage
    try:
        with _stage(job, "transcribing"):
            job.status = JobStatus.TRANSCRIBING
            update(job)
            words, segments = _load_saved_transcript(job, job_dir) or _transcribe(job, source_path)
            _write_json(job_dir / "transcript.json", [w.model_dump() for w in words])
            _write_json(job_dir / "segments.json", [s.model_dump() for s in segments])
            job.transcript_path = str(job_dir / "transcript.json")

        with _stage(job, "deciding"):
            job.status = JobStatus.DECIDING
            job.progress_current = 0
            job.progress_total = 0
            update(job)
            caller.start_stage("decide", settings.decide_max_wall_s)
            judgments = judge_segments(
                segments,
                caller=caller,
                highlights_criteria=opts.highlights_criteria,
                on_progress=_progress_updater(job, update),
                persist_path=job_dir / "decisions.json",
            )
            judgments, repaired = repair_fragments(judgments)
            if repaired:
                logger.info("job %s: kept %d sentence fragment(s) that finish kept sentences", job.job_id, repaired)
            job.decisions_path = str(job_dir / "decisions.json")
            moments, rerank_note = _rerank(job_dir, judgments, caller, opts, settings)
            moments = calibrate_by_rank(moments, settings.highlights_min_raw_score)
            job.llm_usage = _add_usage(prior_usage, caller.usage.as_dict())
            if rerank_note:
                job.warnings.append(rerank_note)

        with _stage(job, "building_edl"):
            job.status = JobStatus.BUILDING_EDL
            job.progress_current = 0
            job.progress_total = 0
            update(job)
            duration = media.video_duration or video_info.duration
            transitions = settings.transitions_enabled if opts.transitions is None else opts.transitions
            fade = fade_frames_for(media.fps, settings.transition_target_s) if transitions else 0
            edl = build_edl(
                [j.to_decision() for j in judgments],
                words,
                duration,
                fps=media.fps,
                fade_frames=fade,
                total_frames=media.total_frames,
            )
            _write_json(job_dir / "edl.json", edl.model_dump())
            job.edl_path = str(job_dir / "edl.json")
            removed = complement(edl)
            label_removed(removed, judgments, words)
            _write_json(job_dir / "edl_removed.json", [r.model_dump() for r in removed])
            job.removed_edl_path = str(job_dir / "edl_removed.json")
            if edl.used_full_video_fallback:
                job.warnings.append("nothing was kept after filtering; the output is the full video")
            reel, reel_edl, shorts = _select(job, job_dir, judgments, moments, words, media, fade, duration, opts,
                                             settings)

        if opts.decide_only:
            job.status = JobStatus.DECIDED
            update(job)
            return

        def chapter_fn(clean_words: list[Word], cleaned_s: float):
            # chapters get their own time budget: the decide one is long gone
            # after rendering
            caller.start_stage("chapters", settings.chapters_max_wall_s)
            try:
                return generate_chapters(clean_words, cleaned_s, caller=caller)
            finally:
                job.llm_usage = _add_usage(prior_usage, caller.usage.as_dict())

        deliver(job, update, DeliverInputs(
            render=RenderInputs(
                job_dir=job_dir,
                source=source_path,
                media=media,
                edl=edl,
                removed=removed,
                reel_edl=reel_edl,
                shorts=shorts,
                words=words,
                title=job.title or title_from_filename(source_path.name),
            ),
            reel=reel,
            chapter_fn=chapter_fn if settings.chapters_enabled else None,
        ))
    except Exception as exc:
        logger.exception("job %s failed", job.job_id)
        job.llm_usage = _add_usage(prior_usage, caller.usage.as_dict())
        _fail(job, update, exc)


# ------------------------------------------------------------------ stages


def _transcribe(job: JobRecord, source_path: Path) -> tuple[list[Word], list[Segment]]:
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
        _, segments = native
        # Work at sentence granularity throughout, not the original
        # per-cue granularity — a cue is a caption-display unit, not a
        # meaningful cut boundary (see transcribe.native for why), so
        # EDL snapping needs these finer, real sentence boundaries too.
        words = segments_to_words(segments)
        words = flag_overlaps(words)
        for seg, w in zip(segments, words, strict=True):
            seg.overlap_candidate = w.overlap_candidate
        return words, segments

    job.transcript_source = "asr"
    words = transcribe(source_path)
    words = flag_overlaps(words)
    return words, build_segments(words)


def _load_saved_transcript(job: JobRecord, job_dir: Path):
    """A job that already transcribed (a decide_only job being rendered)
    reuses its transcript instead of transcribing again."""
    words_path, segments_path = job_dir / "transcript.json", job_dir / "segments.json"
    if not (job.transcript_path and words_path.exists() and segments_path.exists()):
        return None
    try:
        words = [Word.model_validate(w) for w in json.loads(words_path.read_text(encoding="utf-8"))]
        segments = [Segment.model_validate(s) for s in json.loads(segments_path.read_text(encoding="utf-8"))]
    except (OSError, ValueError):
        return None
    if not segments:
        return None
    logger.info("job %s: reusing saved transcript (%d segments)", job.job_id, len(segments))
    return words, segments


def _rerank(job_dir: Path, judgments, caller, opts, settings) -> tuple[list[Moment], str]:
    candidates = candidate_moments(
        judgments,
        floor=settings.highlights_candidate_floor,
        funny_floor=settings.highlights_funny_floor,
        join_gap_s=settings.highlights_join_gap_s,
        max_candidates=settings.rerank_max_candidates,
    )
    if not candidates:
        return [], ""
    highlights_criteria = opts.highlights_criteria or settings.highlights_criteria
    shorts_criteria = opts.shorts_criteria or settings.shorts_criteria
    fingerprint = hashlib.sha256(
        json.dumps({
            "model": caller.model,
            "prompt": rerank_prompt(highlights_criteria, shorts_criteria),
            "candidates": [m.model_dump() for m in candidates],
            "context": settings.rerank_context_segments,
        }, sort_keys=True).encode()
    ).hexdigest()
    saved = job_dir / "rerank.json"
    if saved.exists():
        try:
            data = json.loads(saved.read_text(encoding="utf-8"))
            # only a successful re-rank is reused; a failed one is retried
            if data.get("fingerprint") == fingerprint and data.get("ok"):
                return [Moment.model_validate(m) for m in data["moments"]], data.get("note", "")
        except (OSError, ValueError, KeyError):
            pass
    result = rerank_moments(candidates, judgments, caller=caller, highlights_criteria=highlights_criteria,
                            shorts_criteria=shorts_criteria)
    _write_json(saved, {"fingerprint": fingerprint, "ok": result.ok, "note": result.note,
                        "moments": [m.model_dump() for m in result.moments]})
    return result.moments, result.note


def _select(job, job_dir, judgments, moments, words, media, fade, duration, opts, settings):
    """Choose the highlights reel and the shorts; write selection.json and
    edl_highlights.json. Returns (reel moments, reel EDL or None, shorts)."""
    target = settings.highlights_target_s if opts.highlights_target_s is None else opts.highlights_target_s
    reel = select_highlights(
        moments,
        duration_s=duration,
        target_s=target,
        max_fraction=settings.highlights_max_fraction,
        min_total_s=settings.highlights_min_s,
        min_moment_s=settings.highlights_min_moment_s,
        min_score=settings.highlights_min_score,
        max_share_per_window=settings.highlights_max_share_per_window,
        window_s=settings.highlights_diversity_window_s,
        judgments=judgments,
    )
    reel_edl = None
    if reel:
        reel_edl = build_edl(
            [Decision(start=m.start, end=m.end, decision="keep") for m in reel],
            words,
            duration,
            fps=media.fps,
            fade_frames=fade,
            min_segment_s=settings.highlights_min_moment_s,
            full_video_fallback=False,
            total_frames=media.total_frames,
            min_gap_s=HIGHLIGHT_MIN_GAP_S,
        )
        if not reel_edl.ranges:
            reel_edl = None
    if reel_edl is not None:
        _write_json(job_dir / "edl_highlights.json", reel_edl.model_dump())
    else:
        job.warnings.append("not enough highlight-worthy material for a highlights reel")

    count = settings.shorts_count if opts.shorts_count is None else opts.shorts_count
    shorts = select_shorts(
        moments,
        judgments,
        count=count,
        min_s=settings.shorts_min_s,
        max_s=settings.shorts_max_s,
        preferred_categories=settings.shorts_categories,
    )
    if count and len(shorts) < count:
        job.warnings.append(f"found {len(shorts)} short-worthy moment(s), {count} requested")

    reel_ids = {m.id for m in reel}
    short_ids = {s.moment_id for s in shorts}
    selection = {
        "moments": [
            {**m.model_dump(), "in_highlights": m.id in reel_ids, "in_shorts": m.id in short_ids} for m in moments
        ],
        "highlights": {
            "moment_ids": [m.id for m in reel],
            "total_s": round(sum(r.end - r.start for r in reel_edl.ranges), 2) if reel_edl else 0.0,
            "ranges": [{"start": r.start, "end": r.end} for r in reel_edl.ranges] if reel_edl else [],
        },
        "shorts": [s.model_dump() for s in shorts],
    }
    _write_json(job_dir / "selection.json", selection)
    job.selection_path = str(job_dir / "selection.json")
    return reel, reel_edl, shorts


def label_removed(removed: list[RemovedRange], judgments, words: list[Word]) -> None:
    """Give every removed range a readable reason: the removal category that
    covers most of it, or — for a gap no removed sentence explains, i.e. a
    silence between two kept sentences — "pause (no speech)"."""
    reasons = _removal_reasons(judgments)
    for r in removed:
        r.reason = _reason_for(r.start, r.end, reasons)
        if not r.reason:
            spoken = any(r.start <= (w.start + w.end) / 2 < r.end for w in words)
            r.reason = "trimmed at a cut" if spoken else "pause (no speech)"


def _removal_reasons(judgments) -> list[tuple[float, float, str]]:
    return [(j.start, j.end, j.removal_category) for j in judgments if j.decision == "remove"]


def _reason_for(start: float, end: float, reasons: list[tuple[float, float, str]]) -> str:
    """Readable label for a removed range: the category that covers most of
    its time (e.g. "greetings / small talk")."""
    weight: dict[str, float] = {}
    for s, e, category in reasons:
        overlap = min(e, end) - max(s, start)
        if overlap > 0:
            weight[category] = weight.get(category, 0.0) + overlap
    if not weight:
        return ""
    top = max(weight, key=weight.get)
    return REMOVAL_LABELS.get(top, top.replace("_", " "))


# ------------------------------------------------------------------ helpers


def _add_usage(prior: dict, current: dict) -> dict:
    return {k: round(prior.get(k, 0) + current.get(k, 0), 1) for k in {*prior, *current}}


def _fail(job: JobRecord, update, exc: BaseException, status: JobStatus | None = None, code: str | None = None) -> None:
    err_code, retryable, retry_after = classify(exc)
    job.error_code = code or err_code
    job.retryable = retryable
    job.retry_after_s = retry_after
    job.status = status or (JobStatus.CANCELLED if job.error_code == "cancelled" else JobStatus.FAILED)
    job.error = str(exc)
    update(job)


@contextmanager
def _stage(job: JobRecord, name: str):
    t0 = time.monotonic()
    try:
        yield
    finally:
        job.stage_timings[name] = round(time.monotonic() - t0, 2)


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _progress_updater(job: JobRecord, update: "callable[[JobRecord], None]") -> "callable[[int, int], None]":
    def on_progress(current: int, total: int) -> None:
        job.progress_current = current
        job.progress_total = total
        update(job)

    return on_progress


def run_youtube_pipeline(job: JobRecord, update: "callable[[JobRecord], None]", url: str) -> None:
    """Download a YouTube URL first, then run the normal pipeline against the local file."""
    if not job.source_path or not Path(job.source_path).exists():
        job.status = JobStatus.DOWNLOADING
        update(job)
        try:
            _, path = download_youtube(url)
        except Exception as exc:  # noqa: BLE001
            # download_youtube wraps most failures as YoutubeDownloadError already,
            # but anything it doesn't catch (e.g. yt_dlp itself missing) must still
            # mark the job FAILED rather than escape and leave it stuck DOWNLOADING.
            logger.warning("job %s: youtube download failed: %s", job.job_id, exc)
            _fail(job, update, exc)
            return
        job.source_path = str(path)
        update(job)
    run_pipeline(job, update)
