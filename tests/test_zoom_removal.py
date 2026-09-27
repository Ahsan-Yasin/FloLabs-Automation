"""Zoom ingest was removed. What it left behind must keep working: job records
written while it existed, the database column and the API key scope it added."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import inspect, select, text

from core.config import get_settings
from core.models import JobRecord, JobStatus
from db.models import ApiKey, RefreshToken, User
from db.session import get_engine, run_migrations, session_scope
from services import api_keys, sessions
from services.errors import ServiceError
from tests.product_helpers import make_user


# ------------------------------------------------------------------ old job records
def test_a_finished_zoom_job_still_loads():
    job = JobRecord.model_validate({
        "job_id": "old-zoom", "status": "done", "title": "Robotics sync",
        "zoom_meeting_uuid": "abc==", "zoom_options_hash": "f00", "zoom_meeting": {"topic": "Robotics sync"},
        "transcript_source": "zoom_transcript",
    })
    assert job.status == JobStatus.DONE
    assert job.transcript_source == "uploaded_transcript"
    assert "zoom_meeting_uuid" not in job.model_dump()


def test_a_zoom_job_stuck_waiting_for_its_transcript_becomes_an_interrupted_failure():
    job = JobRecord.model_validate({"job_id": "old-wait", "status": "waiting_transcript", "zoom_meeting_uuid": "abc=="})
    assert job.status == JobStatus.FAILED
    assert job.error_code == "interrupted" and job.retryable is False
    assert "Zoom" in job.error


@pytest.mark.parametrize("code", ["zoom_auth", "zoom_not_found", "zoom_unavailable", "zoom_download_invalid",
                                  "recording_not_ready"])
def test_zoom_only_error_codes_map_to_source_unsupported(code):
    job = JobRecord.model_validate({"job_id": "old-fail", "status": "failed", "error_code": code})
    assert job.error_code == "source_unsupported"


def test_old_job_json_on_disk_loads_through_the_store():
    import api.main as api_main

    job_dir = get_settings().jobs_dir / "0123456789abcdef0123456789abcdef"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(json.dumps({
        "job_id": "0123456789abcdef0123456789abcdef", "status": "done", "zoom_meeting_uuid": "abc==",
        "transcript_source": "zoom_transcript",
    }), encoding="utf-8")
    job = api_main.job_store.get("0123456789abcdef0123456789abcdef")
    assert job is not None and job.status == JobStatus.DONE


# ------------------------------------------------------------------ migration 0002
def test_migration_drops_the_column_and_the_scope_but_keeps_every_row(monkeypatch):
    """The app migrates with SQLite foreign keys ON: a table rebuild of `users`
    would cascade-delete sessions and API keys. They must all survive."""
    monkeypatch.setenv("DB_SCHEMA_MODE", "none")
    get_settings.cache_clear()
    engine = get_engine()
    run_migrations(engine, "0001")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    assert "zoom_host_email" in {c["name"] for c in inspect(engine).get_columns("users")}

    user_id = make_user("ada@example.com")
    with session_scope() as db:
        user = db.get(User, user_id)
        db.execute(text("UPDATE users SET zoom_host_email = 'host@example.com' WHERE id = :id"), {"id": user.id})
        both, _ = api_keys.create(db, user, "both", ["jobs:read"])
        zoom_only, _ = api_keys.create(db, user, "zoom only", ["jobs:read"])
        db.flush()
        both.scopes_json = json.dumps(["jobs:read", "zoom:read"])
        zoom_only.scopes_json = json.dumps(["zoom:read"])
        sessions.issue(db, user, "test", "127.0.0.1")
        both_id, zoom_only_id = both.id, zoom_only.id

    run_migrations(engine, "head")

    assert "zoom_host_email" not in {c["name"] for c in inspect(engine).get_columns("users")}
    with session_scope() as db:
        assert db.get(User, user_id) is not None
        assert db.get(ApiKey, both_id).scopes == ["jobs:read"]
        assert db.get(ApiKey, zoom_only_id).scopes == []
        assert db.scalars(select(RefreshToken).where(RefreshToken.user_id == user_id)).first() is not None


def test_rotating_a_key_drops_the_removed_scope_and_never_widens_an_empty_one():
    user_id = make_user("ada@example.com")
    with session_scope() as db:
        user = db.get(User, user_id)
        both, _ = api_keys.create(db, user, "both", ["jobs:read"])
        empty, _ = api_keys.create(db, user, "was zoom only", ["jobs:read"])
        db.flush()
        both.scopes_json = json.dumps(["jobs:read", "zoom:read"])
        empty.scopes_json = json.dumps([])
        db.flush()
        replacement, _ = api_keys.rotate(db, user, both.id)
        assert replacement.scopes == ["jobs:read"]
        with pytest.raises(ServiceError) as refused:
            api_keys.rotate(db, user, empty.id)
        assert refused.value.status == 422
        assert empty.revoked_at is None  # refused before anything changed
