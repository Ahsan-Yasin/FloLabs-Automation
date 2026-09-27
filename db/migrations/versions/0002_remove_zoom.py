"""remove Zoom: drop users.zoom_host_email and the zoom:read API key scope

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27 16:40:00
"""
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite 3.35+ drops a plain column in place. Never the batch table
        # rebuild here: the app migrates with foreign_keys=ON, and dropping the
        # old users table would cascade-delete every session and API key.
        op.execute("ALTER TABLE users DROP COLUMN zoom_host_email")
    else:
        op.drop_column("users", "zoom_host_email")

    keys = sa.table("api_keys", sa.column("id", sa.String), sa.column("scopes_json", sa.Text))
    connection = op.get_bind()
    for key_id, raw in connection.execute(sa.select(keys.c.id, keys.c.scopes_json)).all():
        try:
            scopes = json.loads(raw or "[]")
        except ValueError:
            continue
        if isinstance(scopes, list) and "zoom:read" in scopes:
            kept = sorted({s for s in scopes if s != "zoom:read"})
            connection.execute(keys.update().where(keys.c.id == key_id).values(scopes_json=json.dumps(kept)))


def downgrade() -> None:
    op.add_column("users", sa.Column("zoom_host_email", sa.String(length=320), nullable=True))
