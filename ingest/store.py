import shutil
import uuid
from pathlib import Path
from typing import BinaryIO

from core.config import get_settings

ALLOWED_SUFFIXES = {".mp4", ".mkv", ".mov"}


def store_video(filename: str, fileobj: BinaryIO) -> tuple[str, Path]:
    """Persist an uploaded recording to the videos dir. Returns (video_id, path)."""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError(f"unsupported file type {suffix!r}, expected one of {sorted(ALLOWED_SUFFIXES)}")

    settings = get_settings()
    video_id = uuid.uuid4().hex
    dest = settings.videos_dir / f"{video_id}{suffix}"
    with dest.open("wb") as out:
        shutil.copyfileobj(fileobj, out)
    return video_id, dest
