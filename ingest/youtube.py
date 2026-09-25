import uuid
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger

logger = get_logger(__name__)


class YoutubeDownloadError(RuntimeError):
    """Raised when a YouTube URL can't be fetched as a local video file."""


# Leftovers yt-dlp or the job may put next to the download; never the video.
_NOT_THE_VIDEO = {".part", ".ytdl", ".json", ".vtt", ".srt"}


def download_youtube(url: str, dest_dir: Path | None = None) -> tuple[str, Path]:
    """Download a YouTube video. Returns (video_id, path).

    With `dest_dir` (the job folder) the file is saved as
    `dest_dir/source.<ext>`, like an upload, so deleting the job deletes it.
    Without it (the pipeline's current call) it goes to
    videos/<video_id>.<ext>, and api.queue.delete_job_files removes it when its
    job is deleted or swept."""
    import yt_dlp

    settings = get_settings()
    video_id = uuid.uuid4().hex
    if dest_dir is not None:
        out_dir, stem = Path(dest_dir), "source"
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir, stem = settings.videos_dir, video_id
    outtmpl = str(out_dir / f"{stem}.%(ext)s")

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

    matches = sorted(p for p in out_dir.glob(f"{stem}.*") if p.suffix.lower() not in _NOT_THE_VIDEO)
    if not matches:
        raise YoutubeDownloadError(f"download reported success but no output file found for {url}")
    return video_id, matches[0]
