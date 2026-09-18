import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    hf_token: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"

    whisperx_model: str = "small"
    whisperx_device: str = "cpu"
    whisperx_compute_type: str = "int8"
    whisperx_diarize_model: str = "pyannote/speaker-diarization-3.1"

    storage_dir: Path = Path("./storage")

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    # EDL tuning (spec section 3.4)
    min_gap_merge_seconds: float = 0.3
    min_segment_seconds: float = 0.5

    @property
    def videos_dir(self) -> Path:
        return self.storage_dir / "videos"

    @property
    def jobs_dir(self) -> Path:
        return self.storage_dir / "jobs"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.videos_dir.mkdir(parents=True, exist_ok=True)
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
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
