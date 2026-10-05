import os

import pytest

from core.config import get_settings


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Point storage at a per-test temp dir and reset the settings cache.
    Auth and the disk preflight are off unless a test turns them on, so the
    real `.env` can't change test outcomes."""
    # get_settings() puts a real-file FFMPEG_BIN's folder on PATH; restoring
    # PATH after each test keeps a test's fake ffmpeg from shadowing the real
    # one for every test after it.
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    # never spend real tokens from a test: a test that needs a key sets its own
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("HC_API_TOKEN", "")
    monkeypatch.setenv("MIN_FREE_DISK_BYTES", "0")
    monkeypatch.setenv("JOB_RETENTION_HOURS", "0")
    for name in ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET", "ZOOM_HOST_EMAIL"):
        monkeypatch.setenv(name, "")  # never reach the real Zoom account from a test
    monkeypatch.setenv("REQUIRE_NATIVE_TRANSCRIPT", "false")
    # the defaults the tests are written against, whatever the shell or .env
    # says (e.g. CHAPTERS_ENABLED=false while running against real jobs)
    monkeypatch.setenv("CHAPTERS_ENABLED", "true")
    monkeypatch.setenv("SILENCE_CUT_ENABLED", "true")
    # the owner's real intro_outro/ folder must never reach a test: an empty
    # per-test folder (a test that needs clips puts them there)
    monkeypatch.setenv("INTRO_OUTRO_ENABLED", "true")
    monkeypatch.setenv("INTRO_OUTRO_DIR", str(tmp_path / "intro_outro"))
    monkeypatch.setenv("INTRO_FILE", "")
    monkeypatch.setenv("OUTRO_FILE", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
