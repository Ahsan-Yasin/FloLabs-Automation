import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    hf_token: str = ""

    # --- LLM (decide: judge + re-rank; chapters) ----------------------------
    # "openai" (default) or "gemini" (the older provider, kept as a fallback).
    llm_provider: str = "openai"
    openai_api_key: str = ""
    # OpenAI's budget model ($0.10 / 1M input, $0.50 / 1M output, Sept 2026).
    # Judging sentences keep/remove + a 0-10 score needs no big model: a
    # 98-minute meeting is ~100k input + ~15k output tokens, about $0.02.
    openai_model: str = "gpt-6-luna"
    # Reasoning tokens are billed as output and slow every call; the compact
    # one-line-per-sentence answers don't need them. Empty = model default.
    openai_reasoning_effort: str = "none"
    # Client-side limiter (usage tier 1 allows 500 requests/minute).
    openai_rpm: int = 450
    # Per-request HTTP timeout, so a stalled call can never wedge the worker.
    openai_request_timeout_s: float = 120.0
    # Hard cap per answer, so a runaway answer can't run up the bill; a
    # 60-sentence judge answer is ~1k tokens, the re-rank ~3k.
    openai_max_output_tokens: int = 16000
    openai_base_url: str = "https://api.openai.com/v1"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    # Client-side limiter (the free tier allows 15 requests/minute).
    gemini_rpm: int = 14
    gemini_request_timeout_s: float = 120.0
    # The settings below apply to whichever provider is used; they keep their
    # historical gemini_ names because .env files already set them.
    # Sentences judged per call. The exchange is compact (numbered text lines
    # in, one short line per sentence out, ~15 output tokens each), so 60 fit
    # comfortably; fewer calls also means the system prompt is re-sent less
    # often. A truncated answer still splits the chunk automatically.
    gemini_max_segments_per_call: int = 60
    # Read-only neighbours shown on each side of a chunk so sentences at a
    # chunk edge are judged in context.
    gemini_context_segments: int = 3
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
    # Owner (2026-09-25): highlights and shorts are mainly for LEARNING — new
    # things and clear explanations people would want to see; funny moments
    # only when they are really good.
    highlights_criteria: str = (
        "moments people can learn from or would find new and interesting: new architectures or system designs "
        "explained; new features announced or demoed; important concepts or how something works explained "
        "clearly; surprising insights, facts or numbers; decisions and the reasons behind them. Genuinely funny "
        "moments are welcome too, but only when they are really good"
    )
    shorts_criteria: str = (
        "a self-contained moment someone outside the meeting can learn from — a clear explanation of a concept "
        "or of how something works, a new architecture or feature, a useful insight or lesson — or, less often, "
        "a genuinely funny moment that stands on its own"
    )
    # Highlight categories that shorts are drawn from first (then any other
    # moment the re-rank marked short-worthy — e.g. funny ones — by score).
    shorts_categories: list[str] = ["concept", "new_architecture", "new_feature", "insight"]
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
    # At most this share of the reel may be funny moments ("mostly things to
    # learn from, fun parts if they are good").
    highlights_max_funny_share: float = 0.34
    # Candidate moments: sentences scoring >= floor (funny ones from
    # funny_floor) joined across pauses <= join gap / <= 2 bridged lines.
    # Funny moments get no head start any more (was 2).
    highlights_candidate_floor: int = 3
    highlights_funny_floor: int = 3
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

    # --- silence cutting (slice/silence.py) ---------------------------------
    # Transcript timings can't show pauses (a caption cue stays on screen into
    # the next line), so dead air inside a kept sentence used to stay in the
    # video. The source audio is measured in 50 ms windows instead; a run
    # quieter than the recording's own threshold for >= silence_min_s is
    # taken out of the cleaned meeting and the highlights reel.
    silence_cut_enabled: bool = True
    silence_min_s: float = 1.0
    # Left in on each side of a cut, so a word's tail or a breath is never
    # clipped and a shortened pause is a natural ~0.55 s beat: after speech
    # stops (a trailing word fades out more slowly) / before it resumes.
    # Pauses up to ~1.1 s are left alone: what would be cut is shorter than a
    # dissolve can cross.
    silence_pad_after_s: float = 0.30
    silence_pad_before_s: float = 0.25
    # Threshold per recording (slice/silence.py; floor = p5 of the window
    # levels, speech = p95, R = speech - floor): range_fraction * R above
    # room tone, but never within speech_headroom of loud speech nor 10 dB of
    # typical speech (protects quiet talkers); less than min_margin above
    # room tone left = can't tell a pause from a quiet talker, nothing is cut.
    # Measured: -55.2 dBFS on a clean Zoom call, -51.6 on a quiet, noisy
    # room where -45 would cut speech.
    silence_min_margin_db: float = 6.0
    silence_range_fraction: float = 0.35
    silence_speech_headroom_db: float = 18.0

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
    # job folder (YouTube downloads and uploads; never a file elsewhere). EC2: true.
    delete_source_when_done: bool = False

    # --- YouTube -------------------------------------------------------------
    # Downloads are refused above these (a 3-hour 720p meeting is about 2 GB).
    # The API accepts only YouTube links, and yt-dlp runs with its YouTube
    # extractor alone, so a link can never make the server fetch another host.
    youtube_max_bytes: int = 6 * 1024**3
    youtube_max_duration_s: float = 4 * 3600.0

    # --- transcripts -------------------------------------------------------
    # Never transcribe ourselves: a job without a platform transcript (YouTube
    # captions or an uploaded .vtt/.srt) fails "transcript_not_ready" instead
    # of running WhisperX. True on servers without WhisperX; False locally so
    # plain uploads still work.
    require_native_transcript: bool = False

    # --- v2 service / lifecycle (plan D14-D16) -----------------------------
    # Operator key (the pre-accounts shared secret), accepted as X-API-Key on
    # every API route with admin rights. Kept so existing n8n flows keep
    # working; give n8n a per-user API key instead and then empty this.
    hc_api_token: str = ""
    # Jobs allowed in the system at once (running + waiting), across all
    # users. One job renders at a time; the rest wait in order.
    max_queue_depth: int = 10
    heartbeat_interval_s: float = 30.0
    stale_after_s: float = 600.0
    job_max_wall_s: float = 10800.0
    # Refuse to start a job unless this much disk is free (6 GiB default).
    min_free_disk_bytes: int = 6 * 1024**3
    # Delete finished jobs older than this at startup. 0 = never (local dev;
    # EC2 sets 24). A job folder containing a `.keep` file is never deleted.
    job_retention_hours: float = 0.0

    # --- product: site, accounts, API keys, email (plan.MD §11) -----------
    # "dev" (local) or "prod" (a public server: the checks in
    # settings_problems() must pass or the app refuses to start).
    app_env: str = "dev"
    app_name: str = "Highlight Cutter"
    # Public address of the site, used in email links and webhook payloads.
    app_base_url: str = "http://127.0.0.1:8000"
    # Empty = SQLite file storage/app.db. Postgres: postgresql+psycopg://...
    database_url: str = ""
    # migrate (Alembic, default) | create_all (tests) | none (operator runs
    # `python -m db.cli upgrade` by hand)
    db_schema_mode: str = "migrate"
    # Signs access tokens (HS256). At least 32 random characters in prod:
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    # Empty in dev = a random secret kept in storage/dev_jwt_secret.
    jwt_secret: str = ""
    jwt_access_ttl_min: int = 15
    jwt_refresh_ttl_days: int = 30
    # auto = Secure cookies whenever APP_BASE_URL is https
    cookie_secure: str = "auto"
    # Take the client IP from X-Forwarded-For (only behind your own proxy).
    trust_proxy: bool = False
    signup_enabled: bool = True
    # Comma list of email domains allowed to sign up (empty = anyone).
    allowed_signup_domains: str = ""
    # Comma list: these addresses become admins once their email is verified.
    admin_emails: str = ""
    # Local development only: with no HC_API_TOKEN, API calls without any
    # credentials act as the operator (the pre-accounts behaviour). Refused
    # when APP_ENV=prod.
    dev_open_api: bool = False
    # console (dev: printed to the log) | memory (tests) | smtp | resend
    email_backend: str = "console"
    email_from: str = ""
    email_reply_to: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    # starttls | ssl | none
    smtp_tls: str = "starttls"
    email_timeout_s: float = 20.0
    resend_api_key: str = ""
    # Jobs one user may have waiting or running at once (429 too_many_jobs).
    max_queued_per_user: int = 2
    max_api_keys_per_user: int = 10
    rate_limit_enabled: bool = True
    webhooks_enabled: bool = True
    webhook_timeout_s: float = 10.0
    # Allow http:// callback URLs (dev) / private-network hosts (local
    # receivers such as a self-hosted n8n on the same machine or LAN).
    webhook_allow_http: bool = False
    webhook_allow_private: bool = False
    # Free-plan limits for non-admin users (0 = unlimited). Minutes = source
    # recording length.
    plan_jobs_per_month: int = 0
    plan_max_minutes: int = 0
    # The unprefixed pre-/api/v1 routes (/jobs/...) still used by
    # older n8n flows. Turn off once everything calls /api/v1.
    legacy_api_enabled: bool = True
    # Optional animated 3D background on the home page (heavier; off).
    hero_3d: bool = False
    # One JSON object per log line (for log shipping in prod).
    log_json: bool = False
    # Public contact address shown on the about/legal pages.
    contact_email: str = ""

    @property
    def is_prod(self) -> bool:
        return self.app_env.strip().lower() == "prod"

    @property
    def smtp_sender_fallback(self) -> str:
        """With SMTP and no EMAIL_FROM, send as the signed-in mailbox: Gmail and
        most providers only accept their own account as the sender."""
        if self.email_backend.strip().lower() == "smtp" and "@" in self.smtp_user:
            return f"{self.app_name} <{self.smtp_user.strip()}>"
        return ""

    @property
    def email_delivers(self) -> bool:
        """False for console/memory: messages go to the log (or a test list),
        never to anyone's inbox."""
        return self.email_backend.strip().lower() in ("smtp", "resend")

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def allowed_signup_domain_set(self) -> set[str]:
        return {d.strip().lower().lstrip("@") for d in self.allowed_signup_domains.split(",") if d.strip()}

    @property
    def cookie_secure_effective(self) -> bool:
        value = self.cookie_secure.strip().lower()
        if value in ("true", "1", "yes"):
            return True
        if value in ("false", "0", "no"):
            return False
        return self.app_base_url.lower().startswith("https://")

    @property
    def base_url(self) -> str:
        return self.app_base_url.rstrip("/")

    @property
    def videos_dir(self) -> Path:
        return self.storage_dir / "videos"

    @property
    def jobs_dir(self) -> Path:
        return self.storage_dir / "jobs"

    @property
    def transcripts_dir(self) -> Path:
        return self.storage_dir / "transcripts"


def settings_problems(settings: Settings) -> list[str]:
    """What is unsafe or broken for APP_ENV=prod (the app refuses to start
    with any of these); in dev they are only logged as warnings."""
    problems = []
    if len(settings.jwt_secret) < 32:
        problems.append("JWT_SECRET must be set to at least 32 random characters")
    if not settings.app_base_url.lower().startswith("https://"):
        problems.append("APP_BASE_URL must be the public https:// address of the site")
    if settings.email_backend in ("console", "memory"):
        problems.append("EMAIL_BACKEND must be smtp or resend (console/memory never deliver mail)")
    if settings.email_backend == "smtp" and not settings.smtp_host:
        problems.append("SMTP_HOST is empty")
    if settings.email_backend == "resend" and not settings.resend_api_key:
        problems.append("RESEND_API_KEY is empty")
    if not settings.email_from and not settings.smtp_sender_fallback:
        problems.append("EMAIL_FROM is empty (e.g. Highlight Cutter <no-reply@your-domain>)")
    if settings.dev_open_api:
        problems.append("DEV_OPEN_API must be false")
    if settings.webhook_allow_http:
        problems.append("WEBHOOK_ALLOW_HTTP must be false")
    if not settings.cookie_secure_effective:
        problems.append("COOKIE_SECURE must not be false")
    return problems


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
