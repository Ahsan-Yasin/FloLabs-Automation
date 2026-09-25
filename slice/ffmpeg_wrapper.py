"""ffmpeg command builders/runners for the v2 renderer (plan §6).

Every video piece is re-encoded exactly once with the STANDARD profile
(slice/profile.py); stream copy is retired because copy-mode clips start at the
previous keyframe, which put ~78 s of removed content back into the 98-minute
regression job. Cut points are whole source frames:

* video inputs seek half a frame early (`-ss`), so the first frame kept by
  ffmpeg's accurate seek is exactly frame `a`; the filter chain then resets
  timestamps, forces CFR, pads the tail by cloning (so a short source can never
  make a piece a frame short) and trims to exactly N frames;
* audio is cut from a timestamp-normalised 48 kHz FLAC by sample count, with
  cumulative sample boundaries so the total is exact even at 29.97 fps.

Filter graphs are written to a file and passed with `-/filter_complex` (the
old `-filter_complex_script` flag no longer exists in ffmpeg 9, and long
graphs would hit the Windows command-line limit).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.proc import run_checked
from core.timeline import AUDIO_RATE, rate_str, ts

from .plan import InputSpan, VideoPart
from .profile import AUDIO_ARGS, BASE_ARGS, ffmpeg_timeout, video_args

logger = get_logger(__name__)

# Samples read on each side of an audio cut so the accurate-seek rounding
# (microsecond -ss) never eats into the samples we keep.
AUDIO_SEEK_MARGIN = 480  # 10 ms @ 48 kHz
MICRO_FADE_SAMPLES = 960  # 20 ms


def ffmpeg_bin() -> str:
    return get_settings().ffmpeg_bin


# --------------------------------------------------------------------- video


def video_input_args(src: Path, span: InputSpan, fps: Fraction) -> list[str]:
    """Seek half a frame before `span.start` so frame `span.start` is the first
    one kept, and read a few frames past the end (the graph trims exactly)."""
    args: list[str] = []
    if span.start > 0:
        args += ["-ss", ts(Fraction(2 * span.start - 1, 2) / fps)]
    args += ["-t", ts(Fraction(span.frames + 3) / fps), "-i", str(src)]
    return args


def video_input_chain(k: int, span: InputSpan, fps: Fraction, width: int, height: int, label: str) -> str:
    return (
        f"[{k}:v]setpts=PTS-STARTPTS,fps={rate_str(fps)},scale={width}:{height},setsar=1,format=yuv420p,"
        f"tpad=stop_mode=clone:stop=3,trim=end_frame={span.frames},setpts=PTS-STARTPTS[{label}]"
    )


def build_video_part_graph(part: VideoPart, fps: Fraction, width: int, height: int) -> str:
    """Filter graph for one batch/seam: per-input normalise+trim, then an
    xfade chain (dissolves) or a concat (hard cuts). Output label [vout]."""
    n = len(part.inputs)
    lines = []
    for k, span in enumerate(part.inputs):
        label = "vout" if n == 1 else f"v{k}"
        lines.append(video_input_chain(k, span, fps, width, height, label))
    if n > 1 and part.join == "concat":
        lines.append("".join(f"[v{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0[vout]")
    elif n > 1:
        d = Fraction(part.fade_frames) / fps
        prev = "v0"
        for k, off in enumerate(part.xfade_offsets(), start=1):
            out = "vout" if k == n - 1 else f"x{k}"
            lines.append(
                f"[{prev}][v{k}]xfade=transition=fade:duration={ts(d)}:offset={ts(Fraction(off) / fps)}[{out}]"
            )
            prev = out
    return ";\n".join(lines)


def build_video_part_cmd(
    src: Path, part: VideoPart, fps: Fraction, width: int, height: int, graph_path: Path, dest: Path
) -> list[str]:
    graph_path.write_text(build_video_part_graph(part, fps, width, height), encoding="utf-8")
    cmd = [ffmpeg_bin(), *BASE_ARGS]
    for span in part.inputs:
        cmd += video_input_args(src, span, fps)
    cmd += ["-/filter_complex", str(graph_path), "-map", "[vout]", "-an", *video_args(fps),
            "-frames:v", str(part.frames), str(dest)]
    return cmd


def render_video_part(
    src: Path, part: VideoPart, fps: Fraction, width: int, height: int, graph_path: Path, dest: Path
) -> None:
    cmd = build_video_part_cmd(src, part, fps, width, height, graph_path, dest)
    logger.debug("render %s (%d inputs, %d frames): %s", part.kind, len(part.inputs), part.frames, dest.name)
    run_checked(cmd, timeout=ffmpeg_timeout(float(Fraction(part.frames) / fps)))


def concat_copy(pieces: list[Path], dest: Path, list_path: Path, durations_s: list[str] | None = None,
                content_s: float = 0.0) -> None:
    """concat demuxer, stream copy. Callers must assert the pieces share one
    encoder profile first (profile.assert_concat_compatible)."""
    lines = []
    for i, piece in enumerate(pieces):
        lines.append(f"file '{piece.resolve().as_posix()}'")
        if durations_s is not None:
            lines.append(f"duration {durations_s[i]}")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy",
           "-movflags", "+faststart", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(content_s / 20.0))


# --------------------------------------------------------------------- audio


def extract_audio_flac(src: Path, dest: Path, channels: int, source_duration_s: float) -> None:
    """Normalise the source audio ONCE to a gap-free 48 kHz FLAC. Cutting audio
    straight from the mp4 drifted +319 ms over 111 cuts (source pts gaps);
    every deliverable's audio is cut from this file instead."""
    ch = 1 if channels == 1 else 2
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-i", str(src), "-vn", "-af", "aresample=async=1:first_pts=0",
           "-ar", str(AUDIO_RATE), "-ac", str(ch), "-c:a", "flac", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(source_duration_s / 10.0))


@dataclass(frozen=True)
class AudioPiece:
    src_start: int  # sample index in audio.flac
    samples: int  # exact length
    fade_in: bool
    fade_out: bool


def audio_input_args(flac: Path, piece: AudioPiece) -> tuple[list[str], int]:
    pre = min(AUDIO_SEEK_MARGIN, piece.src_start)
    seek = piece.src_start - pre
    args: list[str] = []
    if seek > 0:
        args += ["-ss", ts(Fraction(seek, AUDIO_RATE))]
    args += ["-t", ts(Fraction(pre + piece.samples + AUDIO_SEEK_MARGIN, AUDIO_RATE)), "-i", str(flac)]
    return args, pre


def build_audio_graph(pieces: list[AudioPiece], pres: list[int]) -> str:
    lines = []
    n = len(pieces)
    for k, (piece, pre) in enumerate(zip(pieces, pres)):
        # tail-pad with silence so a piece that runs into the end of the file
        # still yields exactly `samples` samples.
        chain = (f"[{k}:a]apad=pad_len={AUDIO_SEEK_MARGIN},"
                 f"atrim=start_sample={pre}:end_sample={pre + piece.samples},asetpts=PTS-STARTPTS")
        fade = min(MICRO_FADE_SAMPLES, piece.samples // 4)
        if piece.fade_in and fade:
            chain += f",afade=t=in:ss=0:ns={fade}"
        if piece.fade_out and fade:
            chain += f",afade=t=out:ss={piece.samples - fade}:ns={fade}"
        label = "aout" if n == 1 else f"a{k}"
        lines.append(f"{chain}[{label}]")
    if n > 1:
        lines.append("".join(f"[a{k}]" for k in range(n)) + f"concat=n={n}:v=0:a=1[aout]")
    return ";\n".join(lines)


def build_audio_chunk_cmd(flac: Path, pieces: list[AudioPiece], graph_path: Path, dest: Path) -> list[str]:
    cmd = [ffmpeg_bin(), *BASE_ARGS]
    pres = []
    for piece in pieces:
        args, pre = audio_input_args(flac, piece)
        cmd += args
        pres.append(pre)
    graph_path.write_text(build_audio_graph(pieces, pres), encoding="utf-8")
    cmd += ["-/filter_complex", str(graph_path), "-map", "[aout]", "-c:a", "flac", "-ar", str(AUDIO_RATE), str(dest)]
    return cmd


def render_audio_chunk(flac: Path, pieces: list[AudioPiece], graph_path: Path, dest: Path) -> None:
    cmd = build_audio_chunk_cmd(flac, pieces, graph_path, dest)
    total_s = sum(p.samples for p in pieces) / AUDIO_RATE
    run_checked(cmd, timeout=ffmpeg_timeout(total_s / 10.0))


def encode_aac(chunks: list[Path], dest: Path, graph_path: Path, content_s: float = 0.0) -> None:
    """Join the exact FLAC chunks (lossless concat filter) and encode AAC once.
    ffmpeg's AAC encoder is single-threaded (~50x realtime here), so the
    timeout scales with the audio length."""
    cmd = [ffmpeg_bin(), *BASE_ARGS]
    for chunk in chunks:
        cmd += ["-i", str(chunk)]
    if len(chunks) == 1:
        cmd += ["-map", "0:a:0"]
    else:
        graph_path.write_text("".join(f"[{k}:a]" for k in range(len(chunks))) +
                              f"concat=n={len(chunks)}:v=0:a=1[aout]", encoding="utf-8")
        cmd += ["-/filter_complex", str(graph_path), "-map", "[aout]"]
    cmd += [*AUDIO_ARGS, "-vn", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(content_s / 10.0))


def mux(video: Path, audio: Path, dest: Path, content_s: float = 0.0) -> None:
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
           "-c", "copy", "-movflags", "+faststart", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(content_s / 20.0))
