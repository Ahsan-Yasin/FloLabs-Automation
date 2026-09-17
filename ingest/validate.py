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
            return float(stream["duration"])
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
