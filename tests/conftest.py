import pytest

from core.config import get_settings


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Point storage at a per-test temp dir and reset the settings cache.
    Auth and the disk preflight are off unless a test turns them on, so the
    real `.env` can't change test outcomes."""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("HC_API_TOKEN", "")
    monkeypatch.setenv("MIN_FREE_DISK_BYTES", "0")
    monkeypatch.setenv("JOB_RETENTION_HOURS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
