import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    hf_token: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    # Segments are batched into calls of at most this size so a single response
    # (all decisions for the batch) can't grow past the model's max output
    # tokens and get truncated mid-JSON — see decide/gemini_client.py. 30
    # sentences ≈ 2k output tokens with the v2 multi-label schema.
    gemini_max_segments_per_call: int = 30
    # Client-side limiter (the free tier allows 15 requests/minute).
    gemini_rpm: int = 14
    # The whole decide stage (all chunks + re-rank) must finish within this.
    decide_max_wall_s: float = 1800.0

    # --- v2 highlights / shorts / chapters (plan D4, D6, D7, D10) ----------
    # What counts as "interesting" is the owner's call and will change; these
    # strings are injected verbatim into the prompts.
    highlights_criteria: str = (
        "funny moments; new architectures or system designs being explained; new features being announced "
        "or demoed; important concepts being explained clearly; decisions being made; surprising insights"
    )
    shorts_criteria: str = (
        "funny, light-hearted or surprising moments that make sense on their own without the rest of the "
        "meeting"
    )
    # Highlight categories that shorts are drawn from first (then any other
    # moment the re-rank marked short-worthy, by score).
    shorts_categories: list[str] = ["funny"]
    highlights_target_s: float = 300.0
    # Short meetings: the reel is at most this fraction of the meeting.
    highlights_max_fraction: float = 0.3
    # Less than this much qualifying material -> no reel at all.
    highlights_min_s: float = 60.0
    highlights_min_moment_s: float = 8.0
    highlights_join_gap_s: float = 3.0
    # Selection starts at the first threshold and lowers it until the reel
    # reaches highlights_fill_s (or the budget).
    highlights_thresholds: list[int] = [7, 6, 5, 4]
    highlights_fill_s: float = 180.0
    rerank_max_candidates: int = 60
    rerank_context_segments: int = 3
    shorts_count: int = 4
    shorts_min_s: float = 20.0
    shorts_max_s: float = 60.0
    chapters_enabled: bool = True
    chapter_block_s: float = 50.0

    whisperx_model: str = "small"
    whisperx_device: str = "cpu"
    whisperx_compute_type: str = "int8"
    whisperx_diarize_model: str = "pyannote/speaker-diarization-3.1"

    storage_dir: Path = Path("./storage")

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    # Guards against a hung ffprobe/ffmpeg process wedging the single
    # background-task worker forever (e.g. a corrupt upload, a stalled network
    # stream, or a process that ends up waiting on stdin/stdout).
    ffprobe_timeout_seconds: float = 60.0
    ffmpeg_timeout_seconds: float = 1800.0

    # EDL tuning (spec section 3.4)
    min_gap_merge_seconds: float = 0.3
    min_segment_seconds: float = 0.5

    # --- v2 rendering (plan §6) -------------------------------------------
    # Dissolve between kept parts. The fade is snapped to an EVEN number of
    # source frames (d = 2*round(F*target/2)) so the half-extension h = d/2 is
    # a whole frame count at every fps; 0.5 s -> 12 frames @25, 16 @30.
    transitions_enabled: bool = True
    transition_target_s: float = 0.5
    # Kept ranges separated by less than this are merged back (never shown as
    # a cut). The effective threshold is max(this, (d+1)/F).
    removed_video_min_gap_s: float = 1.0
    # A batch = one ffmpeg xfade graph. ~25 MB RAM + ~20 threads per input, so
    # cap the input count; cap the content too so one command never needs more
    # than the per-command timeout allows.
    xfade_batch_size: int = 20
    render_max_batch_content_s: float = 600.0
    # Per-command ffmpeg timeout = clamp(factor * content_seconds, floor, cap).
    ffmpeg_timeout_per_content_second: float = 3.0
    ffmpeg_timeout_floor_s: float = 120.0
    ffmpeg_timeout_cap_s: float = 1800.0
    audio_inputs_per_command: int = 100
    # drawtext/subtitles need an explicit font file (fontconfig lookups crash
    # the Windows ffmpeg build). Empty = auto-detect DejaVu/Arial.
    font_file: str = ""

    # --- v2 service / lifecycle (plan D14-D16) -----------------------------
    # X-API-Key for every route except /health and the static UI pages.
    # Empty = auth disabled (local dev only; set it on EC2).
    hc_api_token: str = ""
    # Jobs allowed in the system at once (running + waiting). 1 = one at a time.
    max_queue_depth: int = 1
    heartbeat_interval_s: float = 30.0
    stale_after_s: float = 600.0
    job_max_wall_s: float = 10800.0
    # Refuse to start a job unless this much disk is free (6 GiB default).
    min_free_disk_bytes: int = 6 * 1024**3
    # Delete finished jobs older than this at startup. 0 = never (local dev;
    # EC2 sets 24). A job folder containing a `.keep` file is never deleted.
    job_retention_hours: float = 0.0

    @property
    def videos_dir(self) -> Path:
        return self.storage_dir / "videos"

    @property
    def jobs_dir(self) -> Path:
        return self.storage_dir / "jobs"

    @property
    def transcripts_dir(self) -> Path:
        return self.storage_dir / "transcripts"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.videos_dir.mkdir(parents=True, exist_ok=True)
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    settings.transcripts_dir.mkdir(parents=True, exist_ok=True)
    _ensure_ffmpeg_on_path(settings)
    return settings


def _ensure_ffmpeg_on_path(settings: Settings) -> None:
    """Some libraries (WhisperX's internal audio loader) shell out to a bare
    "ffmpeg"/"ffprobe" they resolve via PATH, ignoring our FFMPEG_BIN setting.
    If FFMPEG_BIN/FFPROBE_BIN point at actual files, make sure their directory
    is on this process's PATH so those subprocess calls still find them.
    """
    for bin_path in (settings.ffmpeg_bin, settings.ffprobe_bin):
        path = Path(bin_path)
        if path.is_file():
            bin_dir = str(path.parent)
            if bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
