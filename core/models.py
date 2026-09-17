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


class EDLRange(BaseModel):
    """A single validated KEEP range in the final Edit Decision List (section 3.4)."""

    start: float
    end: float


class EditDecisionList(BaseModel):
    """Sorted, non-overlapping KEEP ranges ready for slicing."""

    ranges: list[EDLRange] = Field(default_factory=list)
    source_duration: float = 0.0


class JobStatus(str, Enum):
    QUEUED = "queued"
    TRANSCRIBING = "transcribing"
    DECIDING = "deciding"
    BUILDING_EDL = "building_edl"
    SLICING = "slicing"
    DONE = "done"
    FAILED = "failed"
    SKIPPED_DESYNC = "skipped_desync"


class JobRecord(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.QUEUED
    source_path: str
    error: str | None = None
    output_video_path: str | None = None
    edl_path: str | None = None
    transcript_path: str | None = None
    clean_transcript_path: str | None = None
