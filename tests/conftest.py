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
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY"):
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
    # one job at a time, as the service tests were written (the product
    # default queues up to 10)
    monkeypatch.setenv("MAX_QUEUE_DEPTH", "1")
    # product layer: a fresh SQLite file per test (inside the temp storage),
    # built straight from the models (fast); mail goes to an in-memory outbox
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DB_SCHEMA_MODE", "create_all")
    monkeypatch.setenv("EMAIL_BACKEND", "memory")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-" + "x" * 40)
    monkeypatch.setenv("ADMIN_EMAILS", "")
    monkeypatch.setenv("SIGNUP_ENABLED", "true")
    monkeypatch.setenv("ALLOWED_SIGNUP_DOMAINS", "")
    monkeypatch.setenv("PLAN_JOBS_PER_MONTH", "0")
    monkeypatch.setenv("PLAN_MAX_MINUTES", "0")
    monkeypatch.setenv("LEGACY_API_ENABLED", "true")
    monkeypatch.setenv("WEBHOOKS_ENABLED", "true")
    monkeypatch.setenv("WEBHOOK_ALLOW_HTTP", "false")
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE", "false")
    # The service tests predate accounts: they call the API without
    # credentials, which the pre-accounts dev mode allows. Tests of the
    # account layer turn this off (see tests/product_helpers.py).
    monkeypatch.setenv("DEV_OPEN_API", "true")
    get_settings.cache_clear()
    _reset_product_state()
    yield
    _reset_product_state()
    from db.session import dispose_all

    dispose_all()
    get_settings.cache_clear()


def _reset_product_state() -> None:
    """In-process caches of the product layer (rate-limit buckets, the user
    cache behind access tokens, the email outbox, pending webhook work) must
    not leak from one test into the next."""
    import importlib

    for module_name in ("api.ratelimit", "services.auth", "services.email", "services.webhooks",
                        "services.jobs_index"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        reset = getattr(module, "reset_for_tests", None)
        if reset is not None:
            reset()
