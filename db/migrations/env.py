"""Alembic environment. Works two ways:

- from the app (db.session.run_migrations): the connection is handed over in
  config.attributes["connection"];
- from the command line (`alembic upgrade head` or `python -m db.cli upgrade`
  in the repo root): the URL comes from the app settings (DATABASE_URL, or
  the SQLite file inside STORAGE_DIR).
"""

from alembic import context
from sqlalchemy import create_engine, pool

from db import models  # noqa: F401 — registers the tables on Base.metadata
from db.base import Base
from db.session import database_url

config = context.config
target_metadata = Base.metadata


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite can't ALTER most things; batch mode rebuilds the table
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
