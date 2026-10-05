import shutil
import uuid
from pathlib import Path
from typing import Any, NamedTuple

from core.config import get_settings
from core.logging import get_logger
from core.naming import upload_date_iso

logger = get_logger(__name__)


class YoutubeDownloadError(RuntimeError):
    """Raised when a YouTube URL can't be fetched as a local video file."""


# Leftovers yt-dlp or the job may put next to the download; never the video.
_NOT_THE_VIDEO = {".part", ".ytdl", ".json", ".vtt", ".srt"}

# JavaScript runtimes yt-dlp can use to solve YouTube's player challenges.
# Current yt-dlp deprecates extracting YouTube without one (formats can go
# missing) but only enables deno by default; our dev box has node and the EC2
# image may have either, so every runtime actually installed is enabled. node
# also needs the solver script from the yt-dlp-ejs package (the yt-dlp[default]
# extra) — deno can fall back to the copy yt-dlp bundles.
_JS_RUNTIMES = ("deno", "node")


class _YtDlpLogger:
    """Routes yt-dlp's warnings into our log instead of stderr. They used to be
    silenced with no_warnings, which is how the missing-JS-runtime deprecation
    went unnoticed. Progress chatter is dropped, and errors go to debug because
    yt-dlp raises right after reporting one and the caller logs that."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        logger.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)


def ydl_base_opts() -> dict[str, Any]:
    """yt-dlp options shared by every YouTube call (video download and caption
    fetch), so the two can't drift apart on runtime or ffmpeg setup."""
    opts: dict[str, Any] = {"quiet": True, "noplaylist": True, "logger": _YtDlpLogger()}

    # Only pin a location when FFMPEG_BIN is an actual path (not just "ffmpeg" on
    # PATH) — otherwise let yt-dlp do its own normal PATH search.
    ffmpeg_path = Path(get_settings().ffmpeg_bin)
    if ffmpeg_path.is_file():
        opts["ffmpeg_location"] = str(ffmpeg_path.parent)

    # Passing js_runtimes replaces yt-dlp's {"deno": {}} default, so list every
    # runtime found; with none found, leave yt-dlp's default (and its warning).
    runtimes = {name: {} for name in _JS_RUNTIMES if shutil.which(name)}
    if runtimes:
        opts["js_runtimes"] = runtimes
    return opts


class YoutubeDownload(NamedTuple):
    video_id: str
    path: Path
    # The video's own title (the job's title card / report heading); "" when
    # yt-dlp doesn't report one.
    title: str
    # The day the video was published, "YYYY-MM-DD" ("" = unknown): the
    # meeting's date in the final video's name (core/naming.py).
    upload_date: str = ""


# H.264 at <= 720p when YouTube has it: every piece is re-encoded anyway, and
# decoding AV1/VP9 or 1080p+ sources costs CPU and disk on a small render box
# for no visible gain in a meeting recording.
_FORMAT = (
    "bestvideo[height<=720][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]"
    "/bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
    "/best[height<=720][ext=mp4]/best[height<=720]/best"
)


def download_youtube(url: str, dest_dir: Path | None = None) -> YoutubeDownload:
    """Download a YouTube video. Returns (video_id, path, title, upload_date).

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
        **ydl_base_opts(),
        "format": _FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "restrictfilenames": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}
    except Exception as exc:  # yt_dlp raises its own DownloadError, but be defensive
        raise YoutubeDownloadError(f"failed to download {url}: {exc}") from exc

    matches = sorted(p for p in out_dir.glob(f"{stem}.*") if p.suffix.lower() not in _NOT_THE_VIDEO)
    if not matches:
        raise YoutubeDownloadError(f"download reported success but no output file found for {url}")
    title = " ".join(str(info.get("title") or "").split())
    upload_date = upload_date_iso(str(info.get("upload_date") or "")) or ""
    return YoutubeDownload(video_id, matches[0], title, upload_date)
