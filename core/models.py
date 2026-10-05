from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Word(BaseModel):
    """A single ASR word with diarization, per spec section 3.1."""

    word: str
    start: float
    end: float
    speaker: str
    overlap_candidate: bool = False


class Segment(BaseModel):
    """A contiguous speaker turn built from adjacent same-speaker words (section 3.3 input)."""

    speaker: str
    start: float
    end: float
    text: str
    overlap_candidate: bool = False


class Decision(BaseModel):
    """One LLM edit decision for a segment, per spec section 3.3 output schema."""

    start: float
    end: float
    decision: Literal["keep", "remove"]
    reason: str = ""


RemovalCategory = Literal[
    "none", "filler", "greeting_small_talk", "ceremony_intros", "housekeeping", "crosstalk", "tangent",
    "repetition", "dead_air",
]
HighlightCategory = Literal[
    "none", "funny", "new_architecture", "new_feature", "concept", "decision", "insight",
]


class SegmentJudgment(BaseModel):
    """One segment's result from the single multi-label decide pass (plan D4):
    keep/remove for the cleaned meeting AND, independently, how good the
    segment would be in the highlights reel (a removed-but-funny greeting can
    still score high)."""

    index: int
    start: float
    end: float
    speaker: str = ""
    text: str = ""
    decision: Literal["keep", "remove"]
    reason: str = ""
    removal_category: RemovalCategory = "none"
    highlight_score: int = Field(default=0, ge=0, le=10)
    highlight_category: HighlightCategory = "none"

    def to_decision(self) -> "Decision":
        return Decision(start=self.start, end=self.end, decision=self.decision, reason=self.reason)


class Moment(BaseModel):
    """A candidate highlight: a run of consecutive segments (source time)."""

    id: int
    start: float
    end: float
    first_index: int
    last_index: int
    score: float
    category: HighlightCategory = "none"
    # the best-scoring sentence (a long moment is trimmed around it)
    peak_index: int | None = None
    # the re-rank's own 0-10 score; `score` is the rank-calibrated one
    raw_score: float | None = None
    title: str = ""
    hook: str = ""
    short_worthy: bool = False
    reranked: bool = False

    @property
    def duration(self) -> float:
        return self.end - self.start


class ShortClip(BaseModel):
    """One vertical short, cut from the SOURCE at [start, end]."""

    index: int
    moment_id: int
    start: float
    end: float
    score: float
    category: HighlightCategory = "none"
    title: str = ""
    hook: str = ""


class Chapter(BaseModel):
    start: float  # seconds on the timeline the chapter list is for
    title: str


class JobOptions(BaseModel):
    """Per-job overrides; None = use the service setting."""

    transitions: bool | None = None
    highlights_target_s: float | None = Field(default=None, ge=0, le=900)
    shorts_count: int | None = Field(default=None, ge=0, le=10)
    highlights_criteria: str | None = None
    shorts_criteria: str | None = None
    # Stop after decisions + selection so the owner can review them cheaply;
    # POST /jobs/{id}/render then renders using the saved decisions.
    decide_only: bool = False
    # Cut stretches where nobody is speaking (None = SILENCE_CUT_ENABLED).
    cut_silence: bool | None = None
    # Put the owner's intro/outro clips around final.mp4 (None = INTRO_OUTRO_ENABLED).
    intro_outro: bool | None = None


class EDLRange(BaseModel):
    """A single validated KEEP range in the final Edit Decision List (section 3.4).

    `start`/`end` are seconds. When the EDL was built on a frame grid (v2),
    `start_frame`/`end_frame` are the exact source frame indices the renderer
    uses (`start == start_frame / fps`); seconds are kept for readability and
    for the legacy code paths.
    """

    start: float
    end: float
    start_frame: int | None = None
    end_frame: int | None = None


class EditDecisionList(BaseModel):
    """Sorted, non-overlapping KEEP ranges ready for slicing."""

    ranges: list[EDLRange] = Field(default_factory=list)
    source_duration: float = 0.0
    # v2 frame-grid metadata (None/0 for a legacy, seconds-only EDL).
    fps: str | None = None
    fade_frames: int = 0
    total_frames: int | None = None
    # Removed gaps that were too short to cut cleanly and were put back.
    merged_gap_count: int = 0
    merged_gap_seconds: float = 0.0
    used_full_video_fallback: bool = False
    # Source stretches taken out of kept ranges only because nobody was
    # speaking (before frame snapping). Text whose midpoint falls in one is
    # still said in the output (see slice/transcript.remap_transcript).
    silence_cuts: list[tuple[float, float]] = Field(default_factory=list)


RemovedTier = Literal["transcript_only", "video"]


class RemovedRange(BaseModel):
    """A stretch of the source that is NOT in the output (the EDL's complement).

    tier "transcript_only": cut, but shorter than `removed_video_min_gap_s`
    or pure silence (nothing to hear), so it is listed in the removed
    transcript/report but not in removed.mp4.
    tier "video": also rendered into removed.mp4 with a label.
    """

    start: float
    end: float
    start_frame: int | None = None
    end_frame: int | None = None
    tier: RemovedTier = "video"
    reason: str = ""
    # nobody was speaking (detected in the audio; pipeline.label_removed)
    silence: bool = False


class RenderPiece(BaseModel):
    """One kept range as rendered: source frames [src_start, src_end) land at
    output frames [out_start, out_start + (src_end - src_start))."""

    src_start_frame: int
    src_end_frame: int
    out_start_frame: int


class RenderManifest(BaseModel):
    """What a renderer actually produced — the timeline every later stage
    (transcript remap, chapters, report) maps through."""

    kind: str
    fps: str
    width: int
    height: int
    fade_frames: int = 0
    pieces: list[RenderPiece] = Field(default_factory=list)
    expected_frames: int = 0
    expected_audio_samples: int = 0
    audio_sample_rate: int = 48000
    measured_frames: int | None = None
    measured_duration_s: float | None = None
    measured_audio_duration_s: float | None = None
    parts: list[dict] = Field(default_factory=list)
    seams: list[dict] = Field(default_factory=list)
    timings_s: dict[str, float] = Field(default_factory=dict)


ArtifactStatus = Literal["ok", "failed", "skipped"]


class ArtifactInfo(BaseModel):
    """One deliverable of a job (plan D12, D18). `path` is relative to the job
    folder and is also the file's path inside bundle.zip. Mandatory artifacts
    failing fails the job; optional ones fail soft (status + reason)."""

    path: str
    kind: Literal["video", "text", "json", "pdf"] = "video"
    mandatory: bool = False
    status: ArtifactStatus = "ok"
    reason: str = ""
    bytes: int | None = None
    sha256: str | None = None
    duration_s: float | None = None
    # False once the file has been deleted after bundling (it is still in the zip)
    on_disk: bool = True


class JobStatus(str, Enum):
    QUEUED = "queued"
    # Zoom jobs queued before Zoom finished the transcript wait for it here
    # (up to transcript_wait_max_s) before downloading.
    WAITING_TRANSCRIPT = "waiting_transcript"
    DOWNLOADING = "downloading"
    TRANSCRIBING = "transcribing"
    DECIDING = "deciding"
    BUILDING_EDL = "building_edl"
    # rendering the cleaned meeting
    SLICING = "slicing"
    RENDERING_HIGHLIGHTS = "rendering_highlights"
    RENDERING_REMOVED = "rendering_removed"
    RENDERING_SHORTS = "rendering_shorts"
    # intro + highlights + title card + cleaned meeting + outro -> final.mp4
    ASSEMBLING = "assembling"
    BUNDLING = "bundling"
    DONE = "done"
    FAILED = "failed"
    REPORTING = "reporting"
    # decide_only jobs stop here; POST /jobs/{id}/render continues them.
    DECIDED = "decided"
    SKIPPED_DESYNC = "skipped_desync"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {JobStatus.DONE, JobStatus.FAILED, JobStatus.SKIPPED_DESYNC, JobStatus.CANCELLED, JobStatus.DECIDED}
)

TranscriptSource = Literal["asr", "youtube_captions", "uploaded_transcript", "zoom_transcript"]

# Machine-readable failure reasons for n8n (plan §11). `retryable` on the job
# says whether re-submitting the same request can succeed.
ErrorCode = Literal[
    "recording_not_ready",
    "transcript_not_ready",
    "busy",
    "insufficient_disk",
    "zoom_auth",
    "zoom_not_found",
    "zoom_download_invalid",
    "source_unsupported",
    "llm_quota_exhausted",
    "llm_error",
    "render_assert_failed",
    "timeout",
    "interrupted",
    "cancelled",
    "internal",
]


class JobRecord(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.QUEUED
    options: JobOptions = Field(default_factory=JobOptions)
    # Meeting topic (Zoom's topic; for uploads, the file name). Shown on the
    # title card and in the report.
    title: str = ""
    # Zoom jobs: the meeting instance UUID (always the UUID, never the numeric
    # id, which means "latest instance" for recurring meetings), a hash of the
    # options for idempotent POST /jobs/zoom, and what Zoom said about it
    # (topic, start_time, host_email, duration_min, parts, auto_delete_date).
    zoom_meeting_uuid: str | None = None
    zoom_options_hash: str | None = None
    zoom_meeting: dict | None = None
    source_path: str = ""
    source_url: str | None = None
    native_transcript_path: str | None = None
    transcript_source: TranscriptSource = "asr"
    # Real, measured progress within the current status (e.g. segments judged so
    # far / total, or clips rendered so far / total) — 0/0 whenever the current
    # stage has no countable unit of work (e.g. transcribing), so the UI can
    # honestly fall back to a plain "in progress" indicator instead of faking a
    # percentage for a stage we can't actually measure.
    progress_current: int = 0
    progress_total: int = 0
    error: str | None = None
    error_code: ErrorCode | None = None
    retryable: bool = False
    retry_after_s: int | None = None
    warnings: list[str] = Field(default_factory=list)
    output_video_path: str | None = None
    edl_path: str | None = None
    removed_edl_path: str | None = None
    render_manifest_path: str | None = None
    transcript_path: str | None = None
    clean_transcript_path: str | None = None
    decisions_path: str | None = None
    selection_path: str | None = None
    chapters_path: str | None = None
    # Deliverables by name ("final.mp4", "shorts/short_01.mp4", ...) and the zip.
    artifacts: dict[str, ArtifactInfo] = Field(default_factory=dict)
    bundle_path: str | None = None
    bundle_bytes: int | None = None
    bundle_sha256: str | None = None
    # Where the cleaned meeting starts in final.mp4 (intro + highlights reel + title card).
    final_offset_s: float = 0.0
    # Length of the intro / outro clips in final.mp4 (0 = none).
    intro_s: float = 0.0
    outro_s: float = 0.0
    stage_timings: dict[str, float] = Field(default_factory=dict)
    # LLM calls/tokens/rate-limit waits for this job.
    llm_usage: dict[str, float] = Field(default_factory=dict)
    # Set by JobStore: created once, bumped on every persisted update (and by
    # the heartbeat while a long ffmpeg command runs).
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Computed on read by the API: running but no heartbeat for stale_after_s.
    stale: bool = False
