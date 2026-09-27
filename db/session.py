"""Engine and session management.

One engine per database URL. The default URL lives inside STORAGE_DIR
(`sqlite:///<storage>/app.db`), so every test (each with its own temporary
storage folder) gets its own database without any extra wiring.

The schema is applied the first time an engine is created for a URL:
DB_SCHEMA_MODE=migrate (default) runs the Alembic migrations, create_all
builds the tables straight from the models (tests: much faster), none leaves
the database alone (an operator runs `python -m db.cli upgrade`).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import get_settings
from core.logging import get_logger

from .base import Base

logger = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
SCHEMA_MODES = ("migrate", "create_all", "none")

_lock = threading.RLock()
_engines: dict[str, Engine] = {}
_makers: dict[str, sessionmaker[Session]] = {}


def database_url(settings=None) -> str:
    settings = settings or get_settings()
    if settings.database_url:
        return settings.database_url
    return "sqlite:///" + (Path(settings.storage_dir).resolve() / "app.db").as_posix()


def _sqlite_path(url: str) -> Path | None:
    prefix = "sqlite:///"
    if url.startswith(prefix) and len(url) > len(prefix) and ":memory:" not in url:
        return Path(url[len(prefix):])
    return None


def _new_engine(url: str) -> Engine:
    if not url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 5})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        # WAL: readers never wait for the writer (the job worker thread writes
        # the jobs index while requests read it); foreign keys are off by
        # default in SQLite.
        cursor = dbapi_connection.cursor()
        for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "foreign_keys=ON", "busy_timeout=5000"):
            cursor.execute(f"PRAGMA {pragma}")
        cursor.close()

    return engine


def run_migrations(engine: Engine, revision: str = "head") -> None:
    """`alembic upgrade <revision>` on this engine (no alembic.ini needed)."""
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def ensure_schema(engine: Engine) -> None:
    mode = get_settings().db_schema_mode
    if mode not in SCHEMA_MODES:
        raise ValueError(f"DB_SCHEMA_MODE must be one of {', '.join(SCHEMA_MODES)} (got {mode!r})")
    if mode == "create_all":
        Base.metadata.create_all(engine)
    elif mode == "migrate":
        run_migrations(engine)


def get_engine() -> Engine:
    url = database_url()
    engine = _engines.get(url)
    if engine is not None:
        return engine
    with _lock:
        engine = _engines.get(url)
        if engine is None:
            path = _sqlite_path(url)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
            from . import models  # noqa: F401 — registers the tables on Base.metadata

            engine = _new_engine(url)
            try:
                ensure_schema(engine)
            except Exception:
                engine.dispose()
                raise
            _engines[url] = engine
            _makers[url] = sessionmaker(bind=engine, expire_on_commit=False)
        return engine


def get_sessionmaker() -> sessionmaker[Session]:
    url = database_url()
    maker = _makers.get(url)
    if maker is None:
        get_engine()
        maker = _makers[url]
    return maker


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session that commits when the block succeeds and rolls back when it
    raises. For code outside a request (the job worker, CLI, startup)."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request. Handlers commit
    explicitly (services call db.commit()), so nothing is written implicitly
    after the response has been built."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def check_db() -> bool:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 — /health must answer, not raise
        logger.warning("database check failed: %s", exc)
        return False


def dispose_all() -> None:
    """Close every pooled connection (tests; before deleting a database)."""
    with _lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()
        _makers.clear()
