"""Renderers that turn an EDL into a finished file and a RenderManifest."""

from __future__ import annotations

import shutil
import time
from fractions import Fraction
from pathlib import Path

from core.config import get_settings
from core.errors import RenderAssertError
from core.logging import get_logger
from core.models import EditDecisionList, RenderManifest, RenderPiece
from core.timeline import AUDIO_RATE, parse_rate, rate_str, samples_at_frame, to_frame

from .ffmpeg_wrapper import (
    AudioPiece,
    concat_copy,
    encode_aac,
    mux,
    render_audio_chunk,
    render_video_part,
)
from .plan import output_offsets, plan_video
from .profile import MediaInfo, assert_concat_compatible, assert_frames, probe_header

logger = get_logger(__name__)

# AAC works in 1024-sample frames; the encoded track may differ from the exact
# FLAC length by up to one frame at the tail.
AAC_TOLERANCE_S = 2048 / AUDIO_RATE


def edl_frame_ranges(edl: EditDecisionList, fps: Fraction) -> list[tuple[int, int]]:
    """Exact frame ranges from an EDL (frame fields if present, else snapped)."""
    out = []
    for r in edl.ranges:
        s = r.start_frame if r.start_frame is not None else to_frame(r.start, fps)
        e = r.end_frame if r.end_frame is not None else to_frame(r.end, fps)
        out.append((s, e))
    return out


def render_cleaned(
    source: Path,
    audio_flac: Path,
    edl: EditDecisionList,
    media: MediaInfo,
    out_path: Path,
    work_dir: Path,
    on_progress: callable[[int, int], None] | None = None,
    kind: str = "cleaned",
) -> RenderManifest:
    """Render the kept ranges of `edl` with dissolves (edl.fade_frames; 0 =
    hard cuts) into `out_path`, asserting the exact frame count at every step.

    Intermediate pieces are deleted as soon as they have been concatenated, and
    `work_dir` is removed at the end, even on failure.
    """
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

    steps_total = len(parts) + 1 + len(audio_chunks) + 2  # parts, concat, audio chunks, aac, mux
    steps_done = 0

    def step() -> None:
        nonlocal steps_done
        steps_done += 1
        if on_progress:
            on_progress(steps_done, steps_total)

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
        ],
    )
    timings: dict[str, float] = {}

    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # ---- video: batches + seams, each asserted, then concat -c copy
        t0 = time.monotonic()
        piece_paths: list[Path] = []
        for idx, part in enumerate(parts):
            dest = work_dir / f"{kind}_part_{idx:03d}.mp4"
            render_video_part(source, part, fps, width, height, work_dir / f"{kind}_part_{idx:03d}.txt", dest)
            assert_frames(dest, part.frames, f"{part.kind} {idx}")
            piece_paths.append(dest)
            step()
        timings["video_parts"] = round(time.monotonic() - t0, 2)

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
        timings["video_concat"] = round(time.monotonic() - t0, 2)

        # ---- audio: exact FLAC chunks -> one AAC encode
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
        audio_path = work_dir / f"{kind}_audio.m4a"
        content_s = float(Fraction(expected_frames) / fps)
        encode_aac(chunk_paths, audio_path, work_dir / f"{kind}_audio_concat.txt", content_s=content_s)
        for p in chunk_paths:
            p.unlink(missing_ok=True)
        step()
        timings["audio"] = round(time.monotonic() - t0, 2)

        # ---- mux + final asserts from the MP4 header only
        t0 = time.monotonic()
        mux(video_path, audio_path, out_path, content_s=content_s)
        video_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
        header = assert_frames(out_path, expected_frames, f"{kind} output")
        want_s = expected_samples / AUDIO_RATE
        if header.audio_duration is None or abs(header.audio_duration - want_s) > AAC_TOLERANCE_S:
            raise RenderAssertError(
                f"{kind} output audio is {header.audio_duration}s, expected {want_s:.6f}s (±{AAC_TOLERANCE_S:.3f})"
            )
        step()
        timings["mux"] = round(time.monotonic() - t0, 2)

        manifest.measured_frames = header.video_frames
        manifest.measured_duration_s = header.video_duration
        manifest.measured_audio_duration_s = header.audio_duration
        manifest.timings_s = timings
        logger.info(
            "%s rendered: %d ranges, %d parts, %d frames (%.2fs), audio %.3fs",
            kind, len(ranges), len(parts), expected_frames, float(Fraction(expected_frames) / fps),
            header.audio_duration or 0.0,
        )
        return manifest
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
