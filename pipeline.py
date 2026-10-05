import bisect
import hashlib
import json
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from core.config import get_settings
from core.errors import InsufficientDisk, PipelineError, classify, is_fatal
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
from core.naming import final_video_name, meeting_date
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
from ingest import zoom
from ingest.store import title_from_filename
from ingest.validate import AVSyncError, validate_video
from ingest.youtube import download_youtube
from outputs import RenderInputs
from slice.intro_outro import find_intro_outro
from slice.profile import MediaInfo, probe_media
from slice.silence import SilenceParams, find_silences, reach_end, trim_to_speech
from transcribe import (
    fetch_youtube_transcript,
    flag_overlaps,
    load_uploaded_transcript,
    segments_to_words,
    transcribe,
)

logger = get_logger(__name__)

HIGHLIGHT_MIN_GAP_S = 2.0
SILENCE_LABEL = "silence (no one speaking)"
# A removed range this close to lying wholly inside detected silence is pure
# silence (its edges are snapped to frames; the silences to 50 ms windows).
PURE_SILENCE_SLACK_S = 0.05


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
    deliver: final.mp4 (intro + reel + card + cleaned meeting + outro),
    removed.mp4, shorts, transcripts, chapters, report.pdf, manifest.json,
    bundle.zip.

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
    missing = (job.zoom_meeting or {}).get("segments_without_video") or 0
    if missing:
        # the output is shorter than the meeting; that must not go unnoticed
        job.warnings.append(f"{missing} Zoom recording segment(s) had no video file to cut and are not in the output")
    job.error = job.error_code = None
    job.retryable = False
    job.retry_after_s = None
    # a re-render must not serve the previous run's outputs if this one fails
    job.artifacts = {}
    job.bundle_path = job.bundle_sha256 = None
    job.bundle_bytes = None
    job.chapters_path = job.output_video_path = None
    job.final_offset_s = job.intro_s = job.outro_s = 0.0

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

    duration = media.video_duration or video_info.duration
    caller = LazyCaller()
    prior_usage = dict(job.llm_usage)  # a rendered decide_only job keeps its decide-run usage
    try:
        with _stage(job, "transcribing"):
            job.status = JobStatus.TRANSCRIBING
            update(job)
            words, segments = _load_saved_transcript(job, job_dir) or _transcribe(job, source_path, duration)
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
            transitions = settings.transitions_enabled if opts.transitions is None else opts.transitions
            fade = fade_frames_for(media.fps, settings.transition_target_s) if transitions else 0
            with _stage(job, "silence"):
                silences = _detect_silences(job, job_dir, source_path, media, duration, opts, settings, words)
            edl = build_edl(
                [j.to_decision() for j in judgments],
                words,
                duration,
                fps=media.fps,
                fade_frames=fade,
                total_frames=media.total_frames,
                silences=silences,
            )
            _write_json(job_dir / "edl.json", edl.model_dump())
            job.edl_path = str(job_dir / "edl.json")
            removed = complement(edl)
            label_removed(removed, judgments, words, silences)
            _write_json(job_dir / "edl_removed.json", [r.model_dump() for r in removed])
            job.removed_edl_path = str(job_dir / "edl_removed.json")
            if silences is not None:
                logger.info("job %s: %d silence cut(s), %.1fs, taken out of the cleaned meeting", job.job_id,
                            len(edl.silence_cuts), sum(e - s for s, e in edl.silence_cuts))
            if edl.used_full_video_fallback:
                job.warnings.append("nothing was kept after filtering; the output is the full video")
            reel, reel_edl, shorts = _select(job, job_dir, judgments, moments, words, media, fade, duration, opts,
                                             settings, silences)

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

        # looked up per job, so a clip the owner swaps in is used by the next job
        clips = find_intro_outro(settings, opts.intro_outro)
        job.warnings.extend(clips.notes)
        title = job.title or title_from_filename(source_path.name)
        # stored before rendering, so a re-render names the video the same way
        job.meeting_date = meeting_date(job, title)
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
                title=title,
                final_name=final_video_name(title, job.meeting_date),
                intro=clips.intro,
                outro=clips.outro,
            ),
            reel=reel,
            chapter_fn=chapter_fn if settings.chapters_enabled else None,
        ))
    except Exception as exc:
        logger.exception("job %s failed", job.job_id)
        job.llm_usage = _add_usage(prior_usage, caller.usage.as_dict())
        _fail(job, update, exc)


# ------------------------------------------------------------------ stages


def _transcribe(job: JobRecord, source_path: Path, duration: float) -> tuple[list[Word], list[Segment]]:
    # Reuse a platform-provided transcript when we have one (Zoom's own VTT, a
    # user-supplied export for uploads, or YouTube's own captions) — skips the
    # WhisperX ASR/diarization pass entirely. Falls back to self-hosted
    # transcription whenever no transcript is available or it fails to parse,
    # unless the server has no WhisperX (require_native_transcript).
    native = None
    if job.native_transcript_path:
        # cues past the end of the media would become cuts with nothing to cut
        native = load_uploaded_transcript(Path(job.native_transcript_path), max_duration_s=duration)
        if native is not None:
            job.transcript_source = "zoom_transcript" if job.zoom_meeting_uuid else "uploaded_transcript"
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

    if get_settings().require_native_transcript:
        # Only a Zoom transcript can still turn up later; captions or an
        # upload that aren't there now never will.
        retryable = job.zoom_meeting_uuid is not None
        raise PipelineError(
            "no usable platform transcript for this recording, and this server does not transcribe itself "
            "(REQUIRE_NATIVE_TRANSCRIPT=true)",
            code="transcript_not_ready",
            retryable=retryable,
            retry_after_s=zoom.NOT_READY_RETRY_AFTER_S if retryable else None,
        )
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


def _detect_silences(job: JobRecord, job_dir: Path, source_path: Path, media: MediaInfo, duration: float, opts,
                     settings, words: list[Word]) -> list[tuple[float, float]] | None:
    """Stretches of the source where nobody is speaking (source seconds, a
    silence reaching the end of the audio runs to the end of the video), or
    None when silence cutting is off for this job or can't be done. The
    threshold is measured over the part the transcript covers (`words`).
    Cached in silences.json, so a re-render doesn't decode the audio again.
    Optional: a failure only means pauses are not shortened."""
    enabled = settings.silence_cut_enabled if opts.cut_silence is None else opts.cut_silence
    if not enabled or not media.has_audio:
        return None
    span = (min(w.start for w in words), max(w.end for w in words)) if words else None
    try:
        found = find_silences(source_path, job_dir / "silences.json", SilenceParams.from_settings(settings),
                              duration, span=span)
    except Exception as exc:
        if is_fatal(exc):
            raise
        logger.exception("job %s: silence detection failed", job.job_id)
        reason = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:200]
        job.warnings.append(f"silence detection failed, pauses were not shortened: {reason}")
        return None
    logger.info("job %s: silence threshold %s dBFS (room tone %s, speech %s loud / %s typical); %d silence(s) "
                ">= %.1fs, %.1fs%s", job.job_id, found.threshold_db, found.floor_db, found.speech_db, found.talk_db,
                len(found.ranges), settings.silence_min_s, found.total_s, f" — {found.note}" if found.note else "")
    if found.note:  # the owner would otherwise only see the pauses come back
        job.warnings.append(f"pauses were not shortened: {found.note}")
    return reach_end(found.ranges, found.audio_s, duration)


def _select(job, job_dir, judgments, moments, words, media, fade, duration, opts, settings, silences=None):
    """Choose the highlights reel and the shorts; write selection.json and
    edl_highlights.json. Returns (reel moments, reel EDL or None, shorts).

    Silence is taken out of the reel too, in a second pass over the finished
    reel ranges with the normal (not the reel's 12 s / 2 s) minimums: with
    those, the short-keep rule widened the pieces straight back over the
    silences. A short is one continuous window, so only the silence at its
    edges is trimmed (down to the padding)."""
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
        max_funny_share=settings.highlights_max_funny_share,
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
        if silences and reel_edl.ranges:
            reel_edl = build_edl(
                [Decision(start=r.start, end=r.end, decision="keep") for r in reel_edl.ranges],
                [],  # already snapped to sentences
                duration,
                fps=media.fps,
                fade_frames=fade,
                full_video_fallback=False,
                total_frames=media.total_frames,
                silences=silences,
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
    if silences:
        shorts = [s.model_copy(update=dict(zip(("start", "end"), trim_to_speech(
            s.start, s.end, silences, settings.silence_pad_after_s, settings.silence_pad_before_s), strict=True)))
            for s in shorts]

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


def label_removed(removed: list[RemovedRange], judgments, words: list[Word],
                  silences: list[tuple[float, float]] | None = None) -> None:
    """Give every removed range a readable reason.

    A range lying wholly inside detected silence is flagged `silence` and
    kept out of removed.mp4 (tier transcript_only): there is nothing to see
    or hear, and a meeting has dozens of shortened pauses. Any other range
    with a removed sentence in it gets the removal category covering most of
    it (e.g. "greetings / small talk"), however much pause was cut beside
    that sentence: the pause is only dead air around it, and "silence" burnt
    into a removed.mp4 clip in which someone speaks would be wrong. A range
    no removed sentence explains is "trimmed at a cut" if a word was cut
    there (its midpoint is in it and not in a detected silence), otherwise
    silence."""
    reasons = _removal_reasons(judgments)
    silences = silences or []
    for r in removed:
        quiet = _covered(r.start, r.end, silences)
        if quiet >= (r.end - r.start) / 2 and r.end - r.start - quiet <= PURE_SILENCE_SLACK_S:
            r.reason, r.silence, r.tier = SILENCE_LABEL, True, "transcript_only"
            continue
        # a frame-rounding sliver of a neighbouring sentence doesn't count
        weight = {k: v for k, v in _category_weights(r.start, r.end, reasons).items() if v > PURE_SILENCE_SLACK_S}
        if weight:
            top = max(weight, key=weight.get)
            r.reason = REMOVAL_LABELS.get(top, top.replace("_", " "))
            continue
        spoken = any(r.start <= m < r.end and not _covered(m, m + 1e-6, silences)
                     for m in ((w.start + w.end) / 2 for w in words))
        r.reason = "trimmed at a cut" if spoken else SILENCE_LABEL


def _removal_reasons(judgments) -> list[tuple[float, float, str]]:
    return [(j.start, j.end, j.removal_category) for j in judgments if j.decision == "remove"]


def _category_weights(start: float, end: float, reasons: list[tuple[float, float, str]]) -> dict[str, float]:
    """Seconds of [start, end] covered by each removal category."""
    weight: dict[str, float] = {}
    for s, e, category in reasons:
        overlap = min(e, end) - max(s, start)
        if overlap > 0:
            weight[category] = weight.get(category, 0.0) + overlap
    return weight


def _covered(start: float, end: float, spans: list[tuple[float, float]]) -> float:
    """Seconds of [start, end] inside the (sorted, non-overlapping) spans."""
    i = max(0, bisect.bisect_right([s for s, _ in spans], start) - 1)
    total = 0.0
    while i < len(spans) and spans[i][0] < end:
        total += max(0.0, min(end, spans[i][1]) - max(start, spans[i][0]))
        i += 1
    return total


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
            download = download_youtube(url)
        except Exception as exc:  # noqa: BLE001
            # download_youtube wraps most failures as YoutubeDownloadError already,
            # but anything it doesn't catch (e.g. yt_dlp itself missing) must still
            # mark the job FAILED rather than escape and leave it stuck DOWNLOADING.
            logger.warning("job %s: youtube download failed: %s", job.job_id, exc)
            _fail(job, update, exc)
            return
        job.source_path = str(download.path)
        # the video's own title goes on the title card and the report, like a
        # Zoom meeting's topic (the storage name is a meaningless hex id)
        job.title = job.title or download.title[:120]
        # the day it was published names the final video (core/naming.py)
        job.meeting_date = job.meeting_date or download.upload_date or None
        update(job)
    run_pipeline(job, update)


def run_zoom_pipeline(job: JobRecord, update: "callable[[JobRecord], None]") -> None:
    """Fetch a Zoom cloud recording (MP4 + Zoom's own transcript) into the job
    folder as source.mp4/source.vtt, then run the normal pipeline on it. A
    re-render whose source was deleted after delivery (delete_source_when_done)
    downloads it again."""
    if not job.source_path or not Path(job.source_path).exists():
        try:
            _fetch_zoom_recording(job, update)
        except Exception as exc:  # noqa: BLE001 — any failure must end the job, not leave it DOWNLOADING
            logger.warning("job %s: Zoom download failed: %s", job.job_id, exc)
            _fail(job, update, exc)
            return
    run_pipeline(job, update)


def _fetch_zoom_recording(job: JobRecord, update: "callable[[JobRecord], None]") -> None:
    settings = get_settings()
    job_dir = settings.jobs_dir / job.job_id
    client = zoom.get_client()
    uuid = job.zoom_meeting_uuid or ""
    meeting = client.get_meeting(uuid)
    # a re-render reuses its saved transcript, so it never waits for one
    need_transcript = not (job.transcript_path and (job_dir / "segments.json").exists())
    if need_transcript and zoom.readiness(meeting) == "transcript_not_ready":
        job.status = JobStatus.WAITING_TRANSCRIPT
        update(job)
        logger.info("job %s: waiting up to %.0fs for Zoom's transcript", job.job_id, settings.transcript_wait_max_s)
        meeting = zoom.wait_for_transcript(
            client, uuid, meeting, max_wait_s=settings.transcript_wait_max_s, poll_s=settings.transcript_poll_s,
            checkpoint=lambda: update(job),
        )
    status = zoom.readiness(meeting)
    if need_transcript and settings.require_native_transcript and status in ("transcript_not_ready", "no_transcript"):
        # fail before downloading gigabytes that could not be transcribed here
        raise zoom.not_ready_error(status)

    job.status = JobStatus.DOWNLOADING
    job.progress_current = job.progress_total = 0
    update(job)
    with _stage(job, "downloading"):
        source, vtt, info = zoom.download_meeting(
            uuid, job_dir, client=client, meeting=meeting, on_progress=_download_progress(job, update)
        )
    job.source_path = str(source)
    job.native_transcript_path = str(vtt) if vtt else None
    job.zoom_meeting = info
    if info.get("topic"):
        job.title = " ".join(info["topic"].split())[:120]
    if vtt is None and need_transcript:
        logger.info("job %s: no Zoom transcript for this recording; it will be transcribed here", job.job_id)
    job.progress_current = job.progress_total = 0
    update(job)


def _download_progress(job: JobRecord, update: "callable[[JobRecord], None]") -> "callable[[int, int], None]":
    """Download progress in MiB, persisted about once a second: often enough
    for the UI, the heartbeat and a cancel to land mid-download, without
    rewriting job.json for every chunk. Purely time-based: Zoom may omit a
    file's size, and a total that is 0 or too small must not turn every chunk
    into a write (the caller persists the end state itself)."""
    last = float("-inf")

    def on_progress(done: int, total: int) -> None:
        nonlocal last
        now = time.monotonic()
        if now - last < 1.0:
            return
        last = now
        job.progress_current = done // 2**20
        job.progress_total = -(-total // 2**20)
        update(job)

    return on_progress
