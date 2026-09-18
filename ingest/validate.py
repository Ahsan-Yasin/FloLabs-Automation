import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger

logger = get_logger(__name__)

# Section 7: "Audio/video desync in source file -> detect and skip processing
# rather than silently producing a bad cut."
DESYNC_THRESHOLD_SECONDS = 1.0


class AVSyncError(ValueError):
    """Raised when the source file has no audio/video track, or the two are desynced."""


def _parse_tag_duration(raw: str) -> float | None:
    """Parse an ffprobe stream tag duration like "00:01:40.123456789" (Matroska's
    DURATION tag) into seconds. Returns None if it isn't in that format."""
    parts = raw.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours = float(parts[0])
        minutes = float(parts[1])
        seconds = float(parts[2])
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


@dataclass
class VideoInfo:
    duration: float
    video_duration: float
    audio_duration: float


def _ffprobe(path: Path) -> dict:
    settings = get_settings()
    result = subprocess.run(
        [
            settings.ffprobe_bin,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=settings.ffprobe_timeout_seconds,
    )
    return json.loads(result.stdout)


def validate_video(path: Path) -> VideoInfo:
    """Probe the file and raise AVSyncError if it can't be safely processed."""
    probe = _ffprobe(path)
    streams = probe.get("streams", [])

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not video_streams:
        raise AVSyncError(f"{path.name}: no video stream found")
    if not audio_streams:
        raise AVSyncError(f"{path.name}: no audio stream found")

    def stream_duration(stream: dict) -> float:
        if "duration" in stream:
            try:
                return float(stream["duration"])
            except (TypeError, ValueError):
                pass

        # Many real-world files (notably Matroska/.mkv, common for screen and
        # meeting recordings) don't populate a per-stream "duration" field at
        # all. Falling straight through to the container's overall duration
        # here would make video_duration == audio_duration == container
        # duration for BOTH streams whenever either is missing "duration",
        # forcing drift to 0.0 and silently passing files that are actually
        # desynced — exactly the failure this function exists to catch.
        # duration_ts * time_base recovers the real per-stream duration from
        # the same ffprobe payload before we give up and fall back.
        duration_ts = stream.get("duration_ts")
        time_base = stream.get("time_base")
        if duration_ts is not None and time_base:
            try:
                num, _, den = str(time_base).partition("/")
                return float(duration_ts) * float(num) / float(den)
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        tag_duration = stream.get("tags", {}).get("DURATION") or stream.get("tags", {}).get("duration")
        if tag_duration:
            parsed = _parse_tag_duration(str(tag_duration))
            if parsed is not None:
                return parsed

        return float(probe.get("format", {}).get("duration", 0.0))

    video_duration = stream_duration(video_streams[0])
    audio_duration = stream_duration(audio_streams[0])
    container_duration = float(probe.get("format", {}).get("duration", max(video_duration, audio_duration)))

    drift = abs(video_duration - audio_duration)
    if drift > DESYNC_THRESHOLD_SECONDS:
        raise AVSyncError(
            f"{path.name}: audio/video duration mismatch of {drift:.2f}s "
            f"(video={video_duration:.2f}s, audio={audio_duration:.2f}s) exceeds "
            f"{DESYNC_THRESHOLD_SECONDS}s threshold"
        )

    return VideoInfo(duration=container_duration, video_duration=video_duration, audio_duration=audio_duration)
