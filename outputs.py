"""Render every video deliverable of a job (plan §6.4, D6-D9, D18).

Order (chosen for disk and for "mandatory first"):
    audio.flac -> cleaned track -> highlights track -> title card
    -> final.mp4 = highlights + card + cleaned   (mandatory)
    -> highlights.mp4 / cleaned.mp4 (optional copies) -> tracks deleted
    -> removed.mp4 (optional) -> shorts (optional) -> audio.flac deleted

Only the cleaned track and final.mp4 are mandatory: anything else that fails
becomes a failed artifact plus a warning and the job still finishes
(partial-success contract, D18). Cancellation, the job wall clock and a full
disk are never softened.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from core.config import get_settings
from core.errors import RenderAssertError, is_fatal
from core.logging import get_logger
from core.models import (
    ArtifactInfo,
    EditDecisionList,
    JobStatus,
    RemovedRange,
    RenderManifest,
    ShortClip,
    Word,
)
from slice.ffmpeg_wrapper import extract_audio_flac
from slice.fonts import find_font
from slice.pipeline import (
    ShortResult,
    Track,
    assemble,
    render_card,
    render_removed,
    render_short_clip,
    render_track,
)
from slice.profile import MediaInfo

logger = get_logger(__name__)

StatusFn = Callable[[JobStatus], None]
ProgressFn = Callable[[int, int], None]


@dataclass
class RenderInputs:
    job_dir: Path
    source: Path
    media: MediaInfo
    edl: EditDecisionList  # the cleaned meeting
    removed: list[RemovedRange]
    reel_edl: EditDecisionList | None
    shorts: list[ShortClip]
    words: list[Word]  # source timeline (captions)
    title: str = ""


@dataclass
class RenderOutputs:
    cleaned: RenderManifest  # the cleaned meeting's own timeline (starts at 0)
    final_path: Path
    final_frames: int
    final_duration_s: float
    # where the cleaned meeting starts in final.mp4 (highlights + card)
    final_offset_s: float
    highlights: RenderManifest | None = None
    card_s: float = 0.0
    removed: RenderManifest | None = None
    shorts: list[ShortResult] = field(default_factory=list)
    artifacts: dict[str, ArtifactInfo] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


def short_name(index: int) -> str:
    return f"shorts/short_{index:02d}.mp4"


def render_outputs(inp: RenderInputs, *, set_status: StatusFn, on_progress: ProgressFn | None = None) -> RenderOutputs:
    settings = get_settings()
    media = inp.media
    job_dir = inp.job_dir
    # everything temporary lives under tmp/, which startup reconciliation
    # wipes after a crash (the FLAC alone is ~0.5-1 GB for a long meeting)
    scratch = job_dir / "tmp"
    work = scratch / "render"
    flac = scratch / "audio.flac"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, ArtifactInfo] = {}
    warnings: list[str] = []
    timings: dict[str, float] = {}
    font = find_font()
    bold_font = find_font(bold=True)
    if font is None:
        warnings.append("no font found: removed-part labels, the title card and burned-in captions are skipped")

    def soft_fail(name: str, what: str, exc: Exception, kind: str = "video") -> None:
        if is_fatal(exc):
            raise exc
        logger.exception("%s failed", what)
        artifacts[name] = ArtifactInfo(path=name, kind=kind, status="failed", reason=_short_reason(exc))
        warnings.append(f"{what} failed: {_short_reason(exc)}")

    tracks: list[Track] = []
    try:
        set_status(JobStatus.SLICING)
        t0 = time.monotonic()
        extract_audio_flac(inp.source, flac, media.audio_channels, media.duration)
        cleaned = render_track(inp.source, flac, inp.edl, media, work / "cleaned", "cleaned", on_progress)
        tracks.append(cleaned)
        timings["cleaned"] = round(time.monotonic() - t0, 2)

        highlights: Track | None = None
        if inp.reel_edl is not None and inp.reel_edl.ranges:
            set_status(JobStatus.RENDERING_HIGHLIGHTS)
            t0 = time.monotonic()
            try:
                highlights = render_track(inp.source, flac, inp.reel_edl, media, work / "highlights", "highlights",
                                          on_progress)
                tracks.append(highlights)
            except Exception as exc:  # noqa: BLE001 — optional artifact
                soft_fail("highlights.mp4", "highlights reel", exc)
            timings["highlights"] = round(time.monotonic() - t0, 2)
        else:
            artifacts["highlights.mp4"] = ArtifactInfo(path="highlights.mp4", status="skipped",
                                                       reason="not enough highlight-worthy material")

        card: Track | None = None
        if highlights is not None and settings.title_card_s > 0 and font is not None:
            try:
                card = render_card(inp.title, media, settings.title_card_s, work / "card", font, bold_font)
                tracks.append(card)
            except Exception as exc:
                if is_fatal(exc):
                    raise
                logger.exception("title card failed")
                warnings.append(f"title card skipped: {_short_reason(exc)}")

        # ---- final.mp4 (mandatory)
        set_status(JobStatus.ASSEMBLING)
        t0 = time.monotonic()
        final_path = job_dir / "final.mp4"
        parts = [t for t in (highlights, card, cleaned) if t is not None]
        try:
            header = assemble(parts, final_path, work / "final", "final")
        except RenderAssertError:
            if card is None:
                raise
            # a card that doesn't concat cleanly must never cost the whole job
            logger.exception("final.mp4 with the title card failed its asserts; retrying without the card")
            warnings.append("title card skipped: it did not match the meeting video's encoding")
            card = None
            parts = [t for t in (highlights, cleaned) if t is not None]
            header = assemble(parts, final_path, work / "final", "final")
        final_frames = sum(t.frames for t in parts)
        offset_frames = sum(t.frames for t in parts[:-1])
        fps = media.fps
        artifacts["final.mp4"] = ArtifactInfo(path="final.mp4", mandatory=True,
                                              duration_s=float(Fraction(final_frames) / fps))
        timings["final"] = round(time.monotonic() - t0, 2)
        cleaned.manifest.measured_frames = cleaned.frames
        cleaned.manifest.measured_duration_s = cleaned.duration_s

        if highlights is not None and settings.highlights_file_enabled:
            try:
                assemble([highlights], job_dir / "highlights.mp4", work / "hl_out", "highlights")
                artifacts["highlights.mp4"] = ArtifactInfo(path="highlights.mp4", duration_s=highlights.duration_s)
            except Exception as exc:  # noqa: BLE001
                soft_fail("highlights.mp4", "highlights.mp4", exc)
        if settings.keep_cleaned_separately:
            try:
                assemble([cleaned], job_dir / "cleaned.mp4", work / "cleaned_out", "cleaned")
                artifacts["cleaned.mp4"] = ArtifactInfo(path="cleaned.mp4", duration_s=cleaned.duration_s)
            except Exception as exc:  # noqa: BLE001
                soft_fail("cleaned.mp4", "cleaned.mp4", exc)
        out = RenderOutputs(
            cleaned=cleaned.manifest,
            final_path=final_path,
            final_frames=final_frames,
            final_duration_s=header.video_duration or float(Fraction(final_frames) / fps),
            final_offset_s=float(Fraction(offset_frames) / fps),
            highlights=highlights.manifest if highlights is not None else None,
            card_s=card.duration_s if card is not None else 0.0,
            artifacts=artifacts,
            warnings=warnings,
            timings=timings,
        )
        for t in tracks:  # free the disk before the optional renders
            t.delete()
        shutil.rmtree(work, ignore_errors=True)

        # ---- removed.mp4 (optional)
        set_status(JobStatus.RENDERING_REMOVED)
        t0 = time.monotonic()
        try:
            out.removed = render_removed(inp.source, flac, inp.removed, media, job_dir / "removed.mp4",
                                         work / "removed", font, on_progress)
            if out.removed is None:
                artifacts["removed.mp4"] = ArtifactInfo(path="removed.mp4", status="skipped",
                                                        reason="nothing removed was long enough to show")
            else:
                artifacts["removed.mp4"] = ArtifactInfo(path="removed.mp4",
                                                        duration_s=out.removed.measured_duration_s)
        except Exception as exc:  # noqa: BLE001
            soft_fail("removed.mp4", "removed-parts video", exc)
        timings["removed"] = round(time.monotonic() - t0, 2)

        # ---- shorts (optional, each on its own)
        if inp.shorts:
            set_status(JobStatus.RENDERING_SHORTS)
            t0 = time.monotonic()
            for n, clip in enumerate(inp.shorts, 1):
                name = short_name(clip.index)
                try:
                    result = render_short_clip(
                        inp.source, flac, clip, media, inp.words, job_dir / name,
                        job_dir / name.replace(".mp4", ".srt"), work / f"short_{clip.index:02d}", font,
                        width=settings.shorts_width, height=settings.shorts_height,
                        burn_captions=settings.shorts_burn_captions,
                    )
                    out.shorts.append(result)
                    artifacts[name] = ArtifactInfo(path=name, duration_s=result.duration_s)
                    artifacts[name.replace(".mp4", ".srt")] = ArtifactInfo(path=name.replace(".mp4", ".srt"),
                                                                           kind="text")
                    warnings.extend(f"short {clip.index}: {note}" for note in result.notes)
                except Exception as exc:  # noqa: BLE001
                    soft_fail(name, f"short {clip.index}", exc)
                if on_progress:
                    on_progress(n, len(inp.shorts))
            timings["shorts"] = round(time.monotonic() - t0, 2)
        return out
    finally:
        for t in tracks:
            t.delete()
        shutil.rmtree(scratch, ignore_errors=True)


def _short_reason(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    first = text[0] if text else type(exc).__name__
    return first[:300]
