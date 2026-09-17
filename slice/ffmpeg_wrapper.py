import subprocess
from pathlib import Path
from typing import Literal

from core.config import get_settings
from core.logging import get_logger

logger = get_logger(__name__)

KEYFRAME_SNAP_TOLERANCE_SECONDS = 0.5


def probe_keyframe_timestamps(path: Path) -> list[float]:
    """Cheap keyframe-only probe (section 4.1) used to decide stream-copy vs re-encode."""
    settings = get_settings()
    result = subprocess.run(
        [
            settings.ffprobe_bin,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "frame=pts_time",
            "-skip_frame", "nokey",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    timestamps = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            timestamps.append(float(line))
    return timestamps


def nearest_keyframe_distance(t: float, keyframes: list[float]) -> float:
    if not keyframes:
        return float("inf")
    import bisect

    idx = bisect.bisect_left(keyframes, t)
    candidates = [k for k in (keyframes[idx - 1] if idx > 0 else None, keyframes[idx] if idx < len(keyframes) else None) if k is not None]
    return min(abs(k - t) for k in candidates)


def choose_mode(start: float, keyframes: list[float]) -> Literal["copy", "reencode"]:
    return "copy" if nearest_keyframe_distance(start, keyframes) <= KEYFRAME_SNAP_TOLERANCE_SECONDS else "reencode"


def extract_segment(src: Path, start: float, end: float, dest: Path, mode: Literal["copy", "reencode"]) -> None:
    settings = get_settings()
    duration = max(end - start, 0.0)

    if mode == "copy":
        cmd = [
            settings.ffmpeg_bin, "-y",
            "-ss", f"{start:.3f}",
            "-i", str(src),
            "-t", f"{duration:.3f}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(dest),
        ]
    else:
        cmd = [
            settings.ffmpeg_bin, "-y",
            "-i", str(src),
            "-ss", f"{start:.3f}",
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac",
            str(dest),
        ]

    logger.debug("extract_segment[%s]: %s", mode, " ".join(cmd))
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def concat_demuxer(clips: list[Path], dest: Path, filelist_path: Path) -> None:
    settings = get_settings()
    filelist_path.write_text(
        "\n".join(f"file '{clip.resolve().as_posix()}'" for clip in clips), encoding="utf-8"
    )
    cmd = [
        settings.ffmpeg_bin, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(filelist_path),
        "-c", "copy",
        str(dest),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def concat_filter(clips: list[Path], dest: Path) -> None:
    """Fallback for mismatched codecs/timestamps (section 4.2) — re-encodes everything."""
    settings = get_settings()
    inputs: list[str] = []
    for clip in clips:
        inputs += ["-i", str(clip)]

    filter_parts = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(len(clips)))
    filter_complex = f"{filter_parts}concat=n={len(clips)}:v=1:a=1[outv][outa]"

    cmd = [
        settings.ffmpeg_bin, "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac",
        str(dest),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def concat_clips(clips: list[Path], dest: Path, filelist_path: Path) -> None:
    try:
        concat_demuxer(clips, dest, filelist_path)
    except subprocess.CalledProcessError as exc:
        logger.warning("concat demuxer failed (%s), falling back to concat filter", exc)
        concat_filter(clips, dest)
