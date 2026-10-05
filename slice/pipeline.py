"""Renderers that turn EDLs into finished files and RenderManifests.

Everything is built from TRACKS: a video-only file of exactly N frames (every
piece re-encoded once with the STANDARD profile) plus the matching audio as
sample-exact FLAC chunks. `assemble` joins one or more tracks into a finished
mp4 — video by `concat -c copy`, audio by ONE AAC encode over all chunks —
and asserts the frame and sample counts from the header before publishing
the file with an atomic rename. So:

    cleaned.mp4    = assemble([cleaned])
    highlights.mp4 = assemble([highlights])
    final.mp4      = assemble([intro, highlights, title card, cleaned, outro])
    removed.mp4    = assemble([removed, with a label on every clip])

and final.mp4's audio is encoded once from the lossless chunks, never from
the other files' AAC (which would add a generation and priming gaps).
"""

from __future__ import annotations

import os
import shutil
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from core.config import get_settings
from core.errors import RenderAssertError
from core.logging import get_logger
from core.models import (
    EditDecisionList,
    EDLRange,
    RemovedRange,
    RenderManifest,
    RenderPiece,
    ShortClip,
    Word,
)
from core.timeline import (
    AUDIO_RATE,
    fmt_clock,
    parse_rate,
    rate_str,
    round_half_up,
    samples_at_frame,
    to_frame,
)

from .captions import ass_document, caption_cues, srt_text
from .ffmpeg_wrapper import (
    AudioPiece,
    audio_input_args,
    build_card_graph,
    build_clip_graph,
    build_short_graph,
    concat_copy,
    drawtext,
    encode_aac,
    mux,
    render_audio_chunk,
    render_card_video,
    render_clip_audio,
    render_clip_video,
    render_short,
    render_silence_flac,
    render_video_part,
    subtitles_filter,
)
from .fonts import font_family
from .plan import InputSpan, output_offsets, plan_video
from .profile import (
    MediaInfo,
    StreamHeader,
    assert_concat_compatible,
    assert_frames,
    probe_header,
    probe_media,
)

logger = get_logger(__name__)

# AAC works in 1024-sample frames; the encoded track may differ from the exact
# FLAC length by up to one frame at the tail.
AAC_TOLERANCE_S = 2048 / AUDIO_RATE

ProgressFn = Callable[[int, int], None]


def edl_frame_ranges(edl: EditDecisionList, fps: Fraction) -> list[tuple[int, int]]:
    """Exact frame ranges from an EDL (frame fields if present, else snapped)."""
    out = []
    for r in edl.ranges:
        s = r.start_frame if r.start_frame is not None else to_frame(r.start, fps)
        e = r.end_frame if r.end_frame is not None else to_frame(r.end, fps)
        out.append((s, e))
    return out


def audio_channels(media: MediaInfo) -> int:
    """Channels of audio.flac (extract_audio_flac keeps mono sources mono)."""
    return 1 if media.audio_channels == 1 else 2


@dataclass
class Track:
    """A rendered but not yet muxed part of a deliverable."""

    kind: str
    fps: Fraction
    video: Path
    audio_chunks: list[Path]
    frames: int
    samples: int
    manifest: RenderManifest
    steps_done: int = 0
    steps_total: int = 0

    @property
    def duration_s(self) -> float:
        return float(Fraction(self.frames) / self.fps)

    def delete(self) -> None:
        self.video.unlink(missing_ok=True)
        for p in self.audio_chunks:
            p.unlink(missing_ok=True)


class _Steps:
    def __init__(self, total: int, on_progress: ProgressFn | None, done: int = 0):
        self.total, self.done, self.on_progress = total, done, on_progress

    def __call__(self) -> None:
        self.done += 1
        if self.on_progress:
            self.on_progress(self.done, self.total)


def render_track(
    source: Path,
    audio_flac: Path,
    edl: EditDecisionList,
    media: MediaInfo,
    work_dir: Path,
    kind: str = "cleaned",
    on_progress: ProgressFn | None = None,
    overlays: dict[int, str] | None = None,
    extra_steps: int = 0,
) -> Track:
    """Render the kept ranges of `edl` with dissolves (edl.fade_frames; 0 =
    hard cuts) into a video-only file + exact FLAC chunks inside `work_dir`,
    asserting the exact frame/sample count of every piece. `overlays` maps a
    range index to an extra filter (e.g. a drawtext label) for that range.

    Intermediate pieces are deleted as soon as they are concatenated; the
    track's own files stay in `work_dir` (the caller removes the folder)."""
    settings = get_settings()
    fps = parse_rate(edl.fps) if edl.fps else media.fps
    d = edl.fade_frames
    ranges = edl_frame_ranges(edl, fps)
    total_src = edl.total_frames or media.total_frames
    width, height = media.even_width, media.even_height
    max_batch_frames = int(settings.render_max_batch_content_s * fps)
    parts = plan_video(ranges, d, settings.xfade_batch_size, max_batch_frames)

    offsets = output_offsets(ranges)
    expected_frames = sum(e - s for s, e in ranges)
    # Audio: hard cut at every kept-range boundary (the dissolve midpoint),
    # 20 ms micro-fades where the cut is inside the source.
    bounds = [samples_at_frame(o, fps) for o in offsets] + [samples_at_frame(expected_frames, fps)]
    audio_pieces = [
        AudioPiece(
            src_start=samples_at_frame(s, fps),
            samples=bounds[i + 1] - bounds[i],
            fade_in=s > 0,
            fade_out=e < total_src,
        )
        for i, (s, e) in enumerate(ranges)
    ]
    expected_samples = bounds[-1]
    chunk = max(1, settings.audio_inputs_per_command)
    audio_chunks = [audio_pieces[i : i + chunk] for i in range(0, len(audio_pieces), chunk)]
    step = _Steps(len(parts) + 1 + len(audio_chunks) + extra_steps, on_progress)

    manifest = RenderManifest(
        kind=kind,
        fps=rate_str(fps),
        width=width,
        height=height,
        fade_frames=d,
        pieces=[RenderPiece(src_start_frame=s, src_end_frame=e, out_start_frame=o)
                for (s, e), o in zip(ranges, offsets)],
        expected_frames=expected_frames,
        expected_audio_samples=expected_samples,
        audio_sample_rate=AUDIO_RATE,
        parts=[{"kind": p.kind, "inputs": len(p.inputs), "frames": p.frames, "join": p.join} for p in parts],
        seams=[
            {
                "out_time_s": float(Fraction(offsets[i + 1]) / fps),
                "src_from_s": float(Fraction(ranges[i][1]) / fps),
                "src_to_s": float(Fraction(ranges[i + 1][0]) / fps),
            }
            for i in range(len(ranges) - 1)
        ] if d else [],
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    # ---- video: batches + seams, each asserted, then concat -c copy
    t0 = time.monotonic()
    piece_paths: list[Path] = []
    for idx, part in enumerate(parts):
        dest = work_dir / f"{kind}_part_{idx:03d}.mp4"
        render_video_part(source, part, fps, width, height, work_dir / f"{kind}_part_{idx:03d}.txt", dest,
                          overlays)
        assert_frames(dest, part.frames, f"{part.kind} {idx}")
        piece_paths.append(dest)
        step()
    manifest.timings_s["video_parts"] = round(time.monotonic() - t0, 2)

    t0 = time.monotonic()
    video_path = work_dir / f"{kind}_video.mp4"
    if len(piece_paths) == 1:
        piece_paths[0].replace(video_path)
    else:
        assert_concat_compatible(piece_paths)
        concat_copy(piece_paths, video_path, work_dir / f"{kind}_concat.txt",
                    content_s=float(Fraction(expected_frames) / fps))
        for p in piece_paths:
            p.unlink(missing_ok=True)
    assert_frames(video_path, expected_frames, f"{kind} video")
    step()
    manifest.timings_s["video_concat"] = round(time.monotonic() - t0, 2)

    # ---- audio: exact FLAC chunks (joined and encoded once in assemble)
    t0 = time.monotonic()
    chunk_paths: list[Path] = []
    for idx, pieces in enumerate(audio_chunks):
        dest = work_dir / f"{kind}_audio_{idx:03d}.flac"
        render_audio_chunk(audio_flac, pieces, work_dir / f"{kind}_audio_{idx:03d}.txt", dest)
        got = probe_header(dest).audio_duration_ts
        want = sum(p.samples for p in pieces)
        if got != want:
            raise RenderAssertError(f"{kind} audio chunk {idx}: {got} samples, expected exactly {want}")
        chunk_paths.append(dest)
        step()
    manifest.timings_s["audio_chunks"] = round(time.monotonic() - t0, 2)
    return Track(kind, fps, video_path, chunk_paths, expected_frames, expected_samples, manifest,
                 step.done, step.total)


def assemble(
    tracks: list[Track],
    out_path: Path,
    work_dir: Path,
    kind: str,
    on_step: Callable[[], None] | None = None,
) -> StreamHeader:
    """Join tracks into `out_path`: video by concat -c copy (asserted
    compatible), audio by one AAC encode over every track's exact chunks,
    then mux and assert exact frames and audio length from the header.

    Muxes inside `work_dir` and publishes with an atomic rename only once
    every assert has passed, so a failed render never leaves a wrong file at
    out_path. The tracks' own files are left for the caller."""
    if not tracks:
        raise ValueError("nothing to assemble")
    fps = tracks[0].fps
    frames = sum(t.frames for t in tracks)
    samples = sum(t.samples for t in tracks)
    content_s = float(Fraction(frames) / fps)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    try:
        if len(tracks) == 1:
            video = tracks[0].video
        else:
            if any(t.fps != fps for t in tracks):
                raise RenderAssertError(f"{kind}: tracks have different frame rates")
            video = work_dir / f"{kind}_joined_video.mp4"
            made.append(video)
            assert_concat_compatible([t.video for t in tracks])
            concat_copy([t.video for t in tracks], video, work_dir / f"{kind}_joined.txt", content_s=content_s)
            assert_frames(video, frames, f"{kind} video")
        audio = work_dir / f"{kind}_audio.m4a"
        made.append(audio)
        encode_aac([c for t in tracks for c in t.audio_chunks], audio, work_dir / f"{kind}_audio_concat.txt",
                   content_s=content_s)
        if on_step:
            on_step()
        muxed = work_dir / f"{kind}_muxed.mp4"
        made.append(muxed)
        mux(video, audio, muxed, content_s=content_s)
        header = assert_frames(muxed, frames, f"{kind} output")
        want_s = samples / AUDIO_RATE
        if header.audio_duration is None or abs(header.audio_duration - want_s) > AAC_TOLERANCE_S:
            raise RenderAssertError(
                f"{kind} output audio is {header.audio_duration}s, expected {want_s:.6f}s (±{AAC_TOLERANCE_S:.3f})"
            )
        os.replace(muxed, out_path)
        if on_step:
            on_step()
        return header
    finally:
        for p in made:
            p.unlink(missing_ok=True)


def _finish_manifest(manifest: RenderManifest, header: StreamHeader) -> RenderManifest:
    manifest.measured_frames = header.video_frames
    manifest.measured_duration_s = header.video_duration
    manifest.measured_audio_duration_s = header.audio_duration
    return manifest


def render_cleaned(
    source: Path,
    audio_flac: Path,
    edl: EditDecisionList,
    media: MediaInfo,
    out_path: Path,
    work_dir: Path,
    on_progress: ProgressFn | None = None,
    kind: str = "cleaned",
) -> RenderManifest:
    """One EDL -> one finished mp4 (render_track + assemble). `work_dir` is
    removed at the end, even on failure."""
    try:
        track = render_track(source, audio_flac, edl, media, work_dir, kind, on_progress, extra_steps=2)
        steps = _Steps(track.steps_total, on_progress, track.steps_done)
        t0 = time.monotonic()
        header = assemble([track], out_path, work_dir, kind, on_step=steps)
        track.manifest.timings_s["assemble"] = round(time.monotonic() - t0, 2)
        logger.info("%s rendered: %d ranges, %d frames (%.2fs), audio %.3fs", kind, len(edl.ranges),
                    track.frames, track.duration_s, header.audio_duration or 0.0)
        return _finish_manifest(track.manifest, header)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# --------------------------------------------------------------- removed.mp4


def removed_label(r: RemovedRange) -> str:
    text = f"Removed {fmt_clock(r.start)}–{fmt_clock(r.end)}"
    return f"{text} · {r.reason}" if r.reason else text


def render_removed(
    source: Path,
    audio_flac: Path,
    removed: list[RemovedRange],
    media: MediaInfo,
    out_path: Path,
    work_dir: Path,
    font: Path | None,
    on_progress: ProgressFn | None = None,
) -> RenderManifest | None:
    """Every removed range long enough to see (tier "video"), hard cuts, each
    labelled with its SOURCE time and reason (plan D8). None when nothing
    qualifies. Without a font the clips are rendered unlabelled."""
    fps = media.fps
    shown = [r for r in removed if r.tier == "video"]
    if not shown:
        return None
    ranges = []
    for r in shown:
        s = r.start_frame if r.start_frame is not None else to_frame(r.start, fps)
        e = r.end_frame if r.end_frame is not None else to_frame(r.end, fps)
        ranges.append(EDLRange(start=float(Fraction(s) / fps), end=float(Fraction(e) / fps), start_frame=s,
                               end_frame=e))
    edl = EditDecisionList(ranges=ranges, source_duration=media.video_duration, fps=rate_str(fps), fade_frames=0,
                           total_frames=media.total_frames)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        overlays = None
        if font is not None:
            size = max(14, round(media.even_height * 0.045))
            overlays = {}
            for i, r in enumerate(shown):
                label = work_dir / f"label_{i:03d}.txt"
                label.write_text(removed_label(r), encoding="utf-8")
                overlays[i] = drawtext(font, label, size=size, y=str(max(8, round(media.even_height * 0.03))))
        track = render_track(source, audio_flac, edl, media, work_dir, "removed", on_progress, overlays,
                             extra_steps=2)
        steps = _Steps(track.steps_total, on_progress, track.steps_done)
        header = assemble([track], out_path, work_dir, "removed", on_step=steps)
        return _finish_manifest(track.manifest, header)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------- title card


def card_text(title: str, width_chars: int = 28, max_lines: int = 3) -> list[str]:
    lines = textwrap.wrap(" ".join(title.split()), width_chars) if title.strip() else []
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: width_chars - 1].rstrip() + "…"
    return lines


def render_card(
    title: str,
    media: MediaInfo,
    seconds: float,
    work_dir: Path,
    font: Path,
    bold_font: Path | None = None,
    subtitle: str = "Full meeting",
) -> Track:
    """A short black card ("<topic>" / "Full meeting") with silent audio,
    encoded with the STANDARD profile and the source's colour tags so it
    concatenates with tracks cut from the source."""
    fps = media.fps
    width, height = media.even_width, media.even_height
    frames = max(1, round_half_up(Fraction(seconds).limit_denominator(1000) * fps))
    samples = samples_at_frame(frames, fps)
    work_dir.mkdir(parents=True, exist_ok=True)
    lines = card_text(title)
    title_file = None
    title_size = round(height * 0.075)
    if lines:
        title_file = work_dir / "card_title.txt"
        title_file.write_text("\n".join(lines), encoding="utf-8")
        # ~0.55 em per character for a sans font: keep the longest line inside 90% of the width
        title_size = max(12, min(title_size, int(0.9 * width / (0.55 * max(len(x) for x in lines)))))
    subtitle_file = work_dir / "card_subtitle.txt"
    subtitle_file.write_text(subtitle, encoding="utf-8")
    graph = build_card_graph(fps=fps, width=width, height=height, frames=frames, font=font,
                             bold_font=bold_font or font, title_file=title_file, subtitle_file=subtitle_file,
                             title_size=title_size, subtitle_size=max(10, round(title_size * 0.6)),
                             color_tags=dict(media.color_tags))
    video = work_dir / "card_video.mp4"
    render_card_video(video, graph, work_dir / "card_graph.txt", fps, frames)
    assert_frames(video, frames, "title card")
    audio = work_dir / "card_audio.flac"
    render_silence_flac(audio, samples, audio_channels(media))
    got = probe_header(audio).audio_duration_ts
    if got != samples:
        raise RenderAssertError(f"title card audio: {got} samples, expected exactly {samples}")
    manifest = RenderManifest(kind="card", fps=rate_str(fps), width=width, height=height, expected_frames=frames,
                              expected_audio_samples=samples)
    return Track("card", fps, video, [audio], frames, samples, manifest)


# ------------------------------------------------------------- intro / outro


def render_clip(path: Path, media: MediaInfo, work_dir: Path, kind: str) -> Track:
    """The owner's intro or outro clip as a track of final.mp4, converted to
    the MEETING's format so `assemble` can join it by concat -c copy: the
    STANDARD profile at the meeting's size and frame rate, fitted inside the
    frame with black bars, the meeting's colour tags, and audio resampled to
    the meeting's channel count (silence if the clip has none). Hard cuts at
    both joins.

    Length: the NEAREST whole frame on the meeting's grid (as a source's own
    frame count is taken, MediaInfo.total_frames), so the clip keeps its
    length to within half a frame; if that rounds up, the last frame is
    held. Its audio is exactly the samples of those frames."""
    clip = probe_media(path)
    fps = media.fps
    width, height = media.even_width, media.even_height
    frames = max(1, round_half_up(Fraction(clip.video_duration) * fps))
    samples = samples_at_frame(frames, fps)
    work_dir.mkdir(parents=True, exist_ok=True)
    video = work_dir / f"{kind}_video.mp4"
    graph = build_clip_graph(fps=fps, width=width, height=height, frames=frames, color_tags=dict(media.color_tags))
    render_clip_video(path, video, graph, work_dir / f"{kind}_graph.txt", fps, frames)
    assert_frames(video, frames, kind)
    audio = work_dir / f"{kind}_audio.flac"
    if clip.has_audio:
        render_clip_audio(path, audio, samples, audio_channels(media))
    else:
        render_silence_flac(audio, samples, audio_channels(media))
    got = probe_header(audio).audio_duration_ts
    if got != samples:
        raise RenderAssertError(f"{kind} audio: {got} samples, expected exactly {samples}")
    manifest = RenderManifest(kind=kind, fps=rate_str(fps), width=width, height=height, expected_frames=frames,
                              expected_audio_samples=samples)
    logger.info("%s %s: %dx%d @ %s -> %d frames (%.3fs) at %dx%d @ %s, %s", kind, path.name, clip.width,
                clip.height, rate_str(clip.fps), frames, float(Fraction(frames) / fps), width, height, rate_str(fps),
                "with audio" if clip.has_audio else "no audio: silence")
    return Track(kind, fps, video, [audio], frames, samples, manifest)


# -------------------------------------------------------------------- shorts


@dataclass
class ShortResult:
    clip: ShortClip
    path: Path
    srt_path: Path
    frames: int
    duration_s: float
    captions: int
    header: StreamHeader | None = None
    notes: list[str] = field(default_factory=list)


def render_short_clip(
    source: Path,
    audio_flac: Path,
    clip: ShortClip,
    media: MediaInfo,
    words: list[Word],
    out_path: Path,
    srt_path: Path,
    work_dir: Path,
    font: Path | None,
    *,
    width: int = 1080,
    height: int = 1920,
    burn_captions: bool = True,
) -> ShortResult:
    """One vertical short cut from the SOURCE at [clip.start, clip.end]
    (plan D7): blurred background, captions burned in (and written as .srt),
    audio from the FLAC with short fades. Frame-exact, asserted."""
    fps = media.fps
    s = min(max(0, to_frame(clip.start, fps)), media.total_frames - 1)
    e = min(max(s + 1, to_frame(clip.end, fps)), media.total_frames)
    frames = e - s
    a0, a1 = samples_at_frame(s, fps), samples_at_frame(e, fps)
    piece = AudioPiece(src_start=a0, samples=a1 - a0, fade_in=True, fade_out=True)
    _, pre = audio_input_args(audio_flac, piece)
    start_s, end_s = float(Fraction(s) / fps), float(Fraction(e) / fps)
    cues = caption_cues(words, start_s, end_s)
    work_dir.mkdir(parents=True, exist_ok=True)
    notes = []
    try:
        srt_tmp = work_dir / "captions.srt"  # published with the short, never alone
        srt_tmp.write_text(srt_text(cues), encoding="utf-8")
        subtitles = ""
        if burn_captions and font is not None:
            ass = work_dir / "captions.ass"
            ass.write_text(ass_document(cues, title=clip.title, duration_s=end_s - start_s,
                                        font_family=font_family(font), width=width, height=height),
                           encoding="utf-8")
            subtitles = subtitles_filter(ass, font.parent)
        elif burn_captions:
            notes.append("no font found: captions not burned in")
        graph = build_short_graph(frames=frames, fps=fps, width=width, height=height, pre=pre,
                                  samples=piece.samples, subtitles=subtitles)
        tmp = work_dir / "short.mp4"
        render_short(source, audio_flac, InputSpan(s, e), piece, graph, work_dir / "short_graph.txt", tmp, fps)
        header = assert_frames(tmp, frames, f"short {clip.index}")
        want_s = piece.samples / AUDIO_RATE
        if header.audio_duration is None or abs(header.audio_duration - want_s) > AAC_TOLERANCE_S:
            raise RenderAssertError(f"short {clip.index} audio is {header.audio_duration}s, expected {want_s:.3f}s")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, out_path)
        os.replace(srt_tmp, srt_path)
        return ShortResult(clip, out_path, srt_path, frames, float(Fraction(frames) / fps), len(cues), header, notes)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
