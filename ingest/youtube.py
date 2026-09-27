import shutil
import uuid
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from core.config import get_settings
from core.logging import get_logger

logger = get_logger(__name__)


class YoutubeDownloadError(RuntimeError):
    """Raised when a YouTube URL can't be fetched as a local video file."""


# Hosts a job's link may name. Anything else is refused before yt-dlp sees it,
# and yt-dlp itself only has its YouTube extractor enabled (ydl_base_opts), so a
# link can never make this server fetch an internal address or another site.
YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtube-nocookie.com", "www.youtube-nocookie.com", "youtu.be",
})
MAX_URL_LENGTH = 2048


def check_youtube_url(url: str) -> str:
    """The link, trimmed, when it is a YouTube video link; ValueError (with a
    message for the user) otherwise."""
    url = (url or "").strip()
    if not url:
        raise ValueError("paste a YouTube link")
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"the link is longer than {MAX_URL_LENGTH} characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("that isn't a valid link") from exc
    if parts.scheme not in ("https", "http") or parts.username or parts.password or port not in (None, 80, 443):
        raise ValueError("only YouTube links (https://www.youtube.com/... or https://youtu.be/...) are supported")
    if (parts.hostname or "").lower().rstrip(".") not in YOUTUBE_HOSTS:
        raise ValueError("only YouTube links (https://www.youtube.com/... or https://youtu.be/...) are supported")
    if parts.path in ("", "/"):
        raise ValueError("that is YouTube's home page; paste the link of one video")
    return url


class _Unsuitable(Exception):
    """Raised from yt-dlp's match filter so the reason reaches the job's error."""


def _refuse_unsuitable(info: dict[str, Any], *, incomplete: bool = False) -> None:
    """yt-dlp match_filter: live streams never end, and very long videos
    would fill the disk; both are refused before anything is downloaded."""
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise _Unsuitable("live streams can't be cut; wait until the stream has ended")
    duration = info.get("duration")
    limit = get_settings().youtube_max_duration_s
    if limit > 0 and isinstance(duration, (int, float)) and duration > limit:
        raise _Unsuitable(f"the video is {duration / 3600:.1f} hours long; the limit is {limit / 3600:.1f} hours")


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
    settings = get_settings()
    opts: dict[str, Any] = {
        "quiet": True,
        "noplaylist": True,
        "logger": _YtDlpLogger(),
        # only YouTube: never the generic extractor, which would fetch any URL
        "allowed_extractors": ["youtube"],
        "match_filter": _refuse_unsuitable,
    }
    if settings.youtube_max_bytes > 0:
        opts["max_filesize"] = settings.youtube_max_bytes

    # Only pin a location when FFMPEG_BIN is an actual path (not just "ffmpeg" on
    # PATH) — otherwise let yt-dlp do its own normal PATH search.
    ffmpeg_path = Path(settings.ffmpeg_bin)
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


# H.264 at <= 720p when YouTube has it: every piece is re-encoded anyway, and
# decoding AV1/VP9 or 1080p+ sources costs CPU and disk on a small render box
# for no visible gain in a meeting recording.
_FORMAT = (
    "bestvideo[height<=720][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]"
    "/bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
    "/best[height<=720][ext=mp4]/best[height<=720]/best"
)


def download_youtube(url: str, dest_dir: Path | None = None) -> YoutubeDownload:
    """Download a YouTube video. Returns (video_id, path, title).

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
        limit_gb = settings.youtube_max_bytes / 1024**3
        raise YoutubeDownloadError(f"no video file was downloaded for {url} (larger than the {limit_gb:.0f} GB "
                                   "limit, or not available)")
    title = " ".join(str(info.get("title") or "").split())
    return YoutubeDownload(video_id, matches[0], title)
