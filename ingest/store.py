import shutil
import uuid
from pathlib import Path
from typing import BinaryIO

from core.config import get_settings

ALLOWED_SUFFIXES = {".mp4", ".mkv", ".mov"}
ALLOWED_TRANSCRIPT_SUFFIXES = {".vtt", ".srt"}


def job_dir(job_id: str) -> Path:
    return get_settings().jobs_dir / job_id


def store_video(filename: str, fileobj: BinaryIO, job_id: str | None = None) -> tuple[str, Path]:
    """Persist an uploaded recording inside its job folder
    (`jobs/{job_id}/source.<ext>`), so deleting the job deletes the source
    and nothing else. Returns (job_id, path)."""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError(f"unsupported file type {suffix!r}, expected one of {sorted(ALLOWED_SUFFIXES)}")

    job_id = job_id or uuid.uuid4().hex
    dest = job_dir(job_id) / f"source{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        shutil.copyfileobj(fileobj, out)
    return job_id, dest


def store_transcript(filename: str, fileobj: BinaryIO, job_id: str | None = None) -> Path:
    """Persist a user-supplied transcript (e.g. exported from Zoom) in the job
    folder (`jobs/{job_id}/source.vtt`), or the shared transcripts dir when no
    job id is given."""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_TRANSCRIPT_SUFFIXES:
        raise ValueError(
            f"unsupported transcript type {suffix!r}, expected one of {sorted(ALLOWED_TRANSCRIPT_SUFFIXES)}"
        )

    if job_id:
        dest = job_dir(job_id) / f"source{suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        dest = get_settings().transcripts_dir / f"{uuid.uuid4().hex}{suffix}"
    with dest.open("wb") as out:
        shutil.copyfileobj(fileobj, out)
    return dest
