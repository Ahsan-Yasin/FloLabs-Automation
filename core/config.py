import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    hf_token: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    # Sentences judged per call. The v2 exchange is compact (numbered text
    # lines in, 1-letter JSON keys out, ~15 output tokens per sentence), so 60
    # fit comfortably; fewer calls also means the system prompt is re-sent
    # less often. A truncated answer still splits the chunk automatically.
    gemini_max_segments_per_call: int = 60
    # Read-only neighbours shown on each side of a chunk so sentences at a
    # chunk edge are judged in context.
    gemini_context_segments: int = 3
    # Client-side limiter (the free tier allows 15 requests/minute).
    gemini_rpm: int = 14
    # Per-request HTTP timeout, so a stalled call can never wedge the worker.
    gemini_request_timeout_s: float = 120.0
    # The decide stage (all chunks + re-rank) must finish within this;
    # chapters (after rendering) get their own, smaller budget.
    decide_max_wall_s: float = 1800.0
    chapters_max_wall_s: float = 300.0
    # A chunk whose kept sentences all score 0 is re-scored once (scores
    # sometimes collapse for a whole chunk); at most this many per job.
    gemini_max_rescores: int = 4

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
    # Reel clips shorter than this are widened with neighbouring sentences.
    highlights_min_moment_s: float = 12.0
    # Candidates are ranked by the re-rank's score and given a calibrated
    # 0-10 score from their rank (the model's own scores swing too much from
    # run to run to use as absolute thresholds). Moments below this calibrated
    # score (~the bottom half) never enter the reel or the shorts, and neither
    # does anything the re-rank itself scored below highlights_min_raw_score.
    highlights_min_score: float = 5.0
    highlights_min_raw_score: float = 2.0
    # No more than this share of the reel may come from any one stretch of
    # highlights_diversity_window_s of the meeting (relaxed if the reel would
    # otherwise stay short).
    highlights_max_share_per_window: float = 0.35
    highlights_diversity_window_s: float = 600.0
    # Candidate moments: sentences scoring >= floor (funny ones from
    # funny_floor) joined across pauses <= join gap / <= 2 bridged lines.
    highlights_candidate_floor: int = 3
    highlights_funny_floor: int = 2
    highlights_join_gap_s: float = 10.0
    rerank_max_candidates: int = 60
    rerank_context_segments: int = 3
    shorts_count: int = 4
    shorts_min_s: float = 20.0
    shorts_max_s: float = 60.0
    chapters_enabled: bool = True

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

    # --- v2 outputs (plan D8, D9, D12, D18) ---------------------------------
    # "<topic> / Full meeting" card between the highlights reel and the
    # cleaned meeting in final.mp4 (0 = none). Only used when there is a reel.
    title_card_s: float = 2.0
    # Also ship the highlights reel on its own (it is the start of final.mp4).
    highlights_file_enabled: bool = True
    # final.mp4 already contains the cleaned meeting; a separate copy doubles
    # disk and upload size, so it is off by default.
    keep_cleaned_separately: bool = False
    shorts_width: int = 1080
    shorts_height: int = 1920
    # Burn captions (and the moment's title) into the shorts; an .srt is
    # shipped next to each short either way.
    shorts_burn_captions: bool = True
    report_enabled: bool = True
    # After bundle.zip is written: False = delete the individual files and keep
    # only the zip + manifest (EC2, where disk is tight); True = keep them so
    # the UI can play them (local).
    serve_individual_artifacts: bool = True
    # Delete the source recording when the job is done, if it lives inside the
    # job folder (Zoom downloads/uploads; never a file elsewhere). EC2: true.
    delete_source_when_done: bool = False

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
