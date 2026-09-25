"""The ONE encoder profile every v2 video/audio piece is made with, plus the
header-only probes and equality asserts that guard every concat (plan D3).

Why one pinned profile: `concat -c copy` only produces a valid stream when every
piece has identical codec parameters (SPS/PPS, fps, size, pix_fmt, audio rate
and layout). A mismatch does NOT make ffmpeg fail — it silently writes a broken
or wrong-speed file — so the parameters are pinned here and asserted before
each concat.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from core.config import get_settings
from core.errors import RenderAssertError, SourceUnsupported
from core.proc import run_checked
from core.timeline import AUDIO_RATE, parse_rate, rate_str, round_half_up

VIDEO_CODEC_ARGS = [
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "24",
    "-profile:v", "high",
    "-pix_fmt", "yuv420p",
]
VIDEO_RATE_CONTROL_ARGS = ["-maxrate", "2500k", "-bufsize", "5000k"]
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_RATE), "-ac", "2"]
BASE_ARGS = ["-hide_banner", "-nostdin", "-loglevel", "error", "-y"]


def video_args(fps: Fraction) -> list[str]:
    """STANDARD video profile (pieces are rendered with -an)."""
    return [*VIDEO_CODEC_ARGS, "-r", rate_str(fps), "-fps_mode", "cfr", *VIDEO_RATE_CONTROL_ARGS]


def ffmpeg_timeout(content_s: float) -> float:
    s = get_settings()
    return min(s.ffmpeg_timeout_cap_s, max(s.ffmpeg_timeout_floor_s, s.ffmpeg_timeout_per_content_second * content_s))


def ffprobe_timeout(source_s: float = 0.0) -> float:
    return 30.0 + 0.01 * max(0.0, source_s)


@dataclass(frozen=True)
class MediaInfo:
    """What the renderer needs to know about a source file."""

    path: Path
    duration: float
    video_duration: float
    fps: Fraction
    width: int
    height: int
    pix_fmt: str
    total_frames: int
    has_audio: bool
    audio_channels: int
    audio_sample_rate: int
    audio_duration: float

    @property
    def even_width(self) -> int:
        return self.width - self.width % 2

    @property
    def even_height(self) -> int:
        return self.height - self.height % 2


def ffprobe_json(path: Path, extra: list[str] | None = None, timeout: float | None = None) -> dict:
    settings = get_settings()
    cmd = [
        settings.ffprobe_bin, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", *(extra or []), str(path),
    ]
    result = run_checked(cmd, timeout=timeout or ffprobe_timeout())
    return json.loads(result.stdout or "{}")


def _stream_duration(stream: dict, fallback: float) -> float:
    for key in ("duration",):
        try:
            return float(stream[key])
        except (KeyError, TypeError, ValueError):
            pass
    ts, tb = stream.get("duration_ts"), stream.get("time_base")
    if ts is not None and tb:
        try:
            return float(ts) * float(Fraction(str(tb)))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return fallback


def _pick_fps(stream: dict) -> Fraction:
    candidates = []
    for key in ("r_frame_rate", "avg_frame_rate"):
        raw = stream.get(key)
        if not raw or raw in ("0/0", "0/1"):
            continue
        try:
            candidates.append(parse_rate(raw))
        except (ValueError, ZeroDivisionError):
            continue
    # r_frame_rate is the stream's base rate; some VFR files report a timebase
    # like 90000/1 there — fall back to the average in that case.
    for rate in candidates:
        if 1 <= rate <= 120:
            return rate
    raise SourceUnsupported(f"cannot determine a usable frame rate from {stream.get('r_frame_rate')!r}")


def probe_media(path: Path) -> MediaInfo:
    data = ffprobe_json(path)
    streams = data.get("streams", [])
    fmt_duration = float(data.get("format", {}).get("duration") or 0.0)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise SourceUnsupported(f"{path.name}: no video stream")
    fps = _pick_fps(video)
    video_duration = _stream_duration(video, fmt_duration)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width < 2 or height < 2:
        raise SourceUnsupported(f"{path.name}: invalid video size {width}x{height}")
    return MediaInfo(
        path=path,
        duration=fmt_duration or video_duration,
        video_duration=video_duration,
        fps=fps,
        width=width,
        height=height,
        pix_fmt=str(video.get("pix_fmt") or ""),
        total_frames=max(1, round_half_up(Fraction(video_duration) * fps)),
        has_audio=audio is not None,
        audio_channels=int(audio.get("channels") or 2) if audio else 0,
        audio_sample_rate=int(audio.get("sample_rate") or 0) if audio else 0,
        audio_duration=_stream_duration(audio, fmt_duration) if audio else 0.0,
    )


@dataclass(frozen=True)
class StreamHeader:
    """Header-only facts about a rendered file (never decodes frames)."""

    video_frames: int | None
    video_duration: float | None
    fps: str | None
    width: int | None
    height: int | None
    pix_fmt: str | None
    extradata_hash: str | None
    audio_duration: float | None
    audio_duration_ts: int | None
    audio_sample_rate: int | None
    audio_channels: int | None
    audio_codec: str | None
    format_duration: float | None


def probe_header(path: Path) -> StreamHeader:
    data = ffprobe_json(path, extra=["-show_data_hash", "md5"])
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    def _int(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    def _float(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    return StreamHeader(
        video_frames=_int(v.get("nb_frames")) if v else None,
        video_duration=_float(v.get("duration")) if v else None,
        fps=v.get("r_frame_rate") if v else None,
        width=_int(v.get("width")) if v else None,
        height=_int(v.get("height")) if v else None,
        pix_fmt=v.get("pix_fmt") if v else None,
        extradata_hash=v.get("extradata_hash") if v else None,
        audio_duration=_float(a.get("duration")) if a else None,
        audio_duration_ts=_int(a.get("duration_ts")) if a else None,
        audio_sample_rate=_int(a.get("sample_rate")) if a else None,
        audio_channels=_int(a.get("channels")) if a else None,
        audio_codec=a.get("codec_name") if a else None,
        format_duration=_float(data.get("format", {}).get("duration")),
    )


def assert_concat_compatible(paths: list[Path], headers: list[StreamHeader] | None = None) -> list[StreamHeader]:
    """Every video piece must share SPS/PPS (extradata md5), fps, size and
    pix_fmt before `concat -c copy`, or the result is silently corrupt."""
    headers = headers or [probe_header(p) for p in paths]
    if not headers:
        return headers
    ref = headers[0]
    for path, h in zip(paths, headers):
        for field in ("extradata_hash", "fps", "width", "height", "pix_fmt"):
            if getattr(h, field) != getattr(ref, field):
                raise RenderAssertError(
                    f"concat piece {path.name} has {field}={getattr(h, field)!r}, "
                    f"expected {getattr(ref, field)!r} (from {paths[0].name})"
                )
        if h.video_frames is None:
            raise RenderAssertError(f"concat piece {path.name} has no video frame count in its header")
    return headers


def assert_frames(path: Path, expected: int, what: str) -> StreamHeader:
    header = probe_header(path)
    if header.video_frames != expected:
        raise RenderAssertError(
            f"{what}: {path.name} has {header.video_frames} video frames, expected exactly {expected}"
        )
    return header
