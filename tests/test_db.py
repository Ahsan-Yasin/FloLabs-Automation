"""The product database layer (plan.MD P0): migrations, types, engines."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from core.config import Settings, get_settings, settings_problems
from db import models
from db.base import Base
from db.cli import main as cli_main
from db.session import database_url, dispose_all, get_engine, session_scope


def test_migrations_build_exactly_the_models_schema(monkeypatch):
    monkeypatch.setenv("DB_SCHEMA_MODE", "migrate")
    get_settings.cache_clear()
    engine = get_engine()
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert context.get_current_revision() == "0002"
        assert compare_metadata(context, Base.metadata) == []
    tables = set(inspect(engine).get_table_names())
    assert {"users", "email_tokens", "refresh_tokens", "api_keys", "jobs_index", "webhook_deliveries"} <= tables


def test_default_database_lives_in_the_storage_folder(tmp_path, monkeypatch):
    first = database_url()
    assert first.startswith("sqlite:///") and first.endswith("/storage/app.db")
    get_engine()
    assert (get_settings().storage_dir / "app.db").exists()

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "other"))
    get_settings.cache_clear()
    assert database_url() != first
    with session_scope() as db:  # a separate, empty database
        assert db.scalars(select(models.User)).all() == []


def test_datetimes_come_back_as_utc():
    karachi = timezone(timedelta(hours=5))
    stamp = datetime(2026, 9, 27, 17, 0, tzinfo=karachi)
    with session_scope() as db:
        db.add(models.User(id="u1", email="a@example.com", password_hash="x", email_verified_at=stamp))
    with session_scope() as db:
        user = db.get(models.User, "u1")
        assert user.email_verified_at == stamp
        assert user.email_verified_at.tzinfo == UTC and user.email_verified_at.hour == 12
        assert user.created_at.tzinfo == UTC
        assert user.webhook_secret.startswith("whsec_") and len(user.webhook_secret) > 30


def test_naive_datetimes_are_refused():
    with pytest.raises(Exception, match="naive datetime"), session_scope() as db:
        db.add(models.User(id="u2", email="b@example.com", password_hash="x",
                           email_verified_at=datetime(2026, 1, 1)))  # noqa: DTZ001 — naive on purpose


def test_api_key_scopes_round_trip():
    with session_scope() as db:
        db.add(models.User(id="u3", email="c@example.com", password_hash="x"))
        key = models.ApiKey(user_id="u3", name="n8n", prefix="ABCDEFGH", key_hash="h" * 64)
        key.scopes = ["jobs:write", "jobs:read", "jobs:read"]
        db.add(key)
    with session_scope() as db:
        stored = db.scalars(select(models.ApiKey)).one()
        assert stored.scopes == ["jobs:read", "jobs:write"]
        assert stored.user.email == "c@example.com"


def test_health_reports_the_database():
    body = TestClient(__import__("api.main", fromlist=["app"]).app).get("/health").json()
    assert body["db"] == "ok"


def test_cli_upgrade_creates_the_database(monkeypatch, capsys):
    monkeypatch.setenv("DB_SCHEMA_MODE", "migrate")
    get_settings.cache_clear()
    dispose_all()
    assert cli_main(["upgrade"]) == 0
    assert (get_settings().storage_dir / "app.db").exists()
    assert cli_main(["current"]) == 0
    assert "revision 0002" in capsys.readouterr().out
    assert cli_main(["stats"]) == 0
    assert "users: 0" in capsys.readouterr().out


def test_prod_settings_are_checked():
    unsafe = Settings(app_env="prod", jwt_secret="short", app_base_url="http://x", email_backend="console",
                      dev_open_api=True)
    problems = " ".join(settings_problems(unsafe))
    for needle in ("JWT_SECRET", "APP_BASE_URL", "EMAIL_BACKEND", "EMAIL_FROM", "DEV_OPEN_API", "COOKIE_SECURE"):
        assert needle in problems
    safe = Settings(app_env="prod", jwt_secret="k" * 40, app_base_url="https://cut.example.com",
                    email_backend="smtp", smtp_host="smtp.example.com", email_from="HC <no-reply@example.com>",
                    dev_open_api=False)
    assert settings_problems(safe) == []
    assert safe.cookie_secure_effective is True
    assert Settings(admin_emails=" A@x.com, b@y.com ,").admin_email_set == {"a@x.com", "b@y.com"}
