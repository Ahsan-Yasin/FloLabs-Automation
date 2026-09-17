import uuid
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger

logger = get_logger(__name__)


class YoutubeDownloadError(RuntimeError):
    """Raised when a YouTube URL can't be fetched as a local video file."""


def download_youtube(url: str) -> tuple[str, Path]:
    """Download a YouTube video to the videos dir. Returns (video_id, path)."""
    import yt_dlp

    settings = get_settings()
    video_id = uuid.uuid4().hex
    outtmpl = str(settings.videos_dir / f"{video_id}.%(ext)s")

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
    }

    # Only pin a location when FFMPEG_BIN is an actual path (not just "ffmpeg" on
    # PATH) — otherwise let yt-dlp do its own normal PATH search.
    ffmpeg_path = Path(settings.ffmpeg_bin)
    if ffmpeg_path.is_file():
        ydl_opts["ffmpeg_location"] = str(ffmpeg_path.parent)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # yt_dlp raises its own DownloadError, but be defensive
        raise YoutubeDownloadError(f"failed to download {url}: {exc}") from exc

    matches = sorted(settings.videos_dir.glob(f"{video_id}.*"))
    if not matches:
        raise YoutubeDownloadError(f"download reported success but no output file found for {url}")
    return video_id, matches[0]
