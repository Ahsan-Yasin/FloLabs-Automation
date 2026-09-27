"""Operator commands for the product database.

    python -m db.cli upgrade                 apply migrations (creates the database)
    python -m db.cli current                 show the applied migration
    python -m db.cli stats                   users / keys / jobs counts
    python -m db.cli create-admin EMAIL      create a verified admin (asks for a password)
    python -m db.cli promote EMAIL           make an existing user an admin
    python -m db.cli demote EMAIL            make an admin a normal user
    python -m db.cli deactivate EMAIL        block a user from logging in
    python -m db.cli activate EMAIL          unblock a user
    python -m db.cli set-password EMAIL      set a user's password (asks for it)
    python -m db.cli index-jobs              index job folders the database doesn't know yet

Settings come from .env like the app's (DATABASE_URL / STORAGE_DIR).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from sqlalchemy import func, select


def _upgrade(_args) -> int:
    from db.session import database_url, get_engine, run_migrations

    engine = get_engine()  # DB_SCHEMA_MODE=migrate (default) already upgrades on creation
    run_migrations(engine)
    print(f"database is up to date: {_redact(database_url())}")
    return 0


def _current(_args) -> int:
    from alembic.runtime.migration import MigrationContext

    from db.session import database_url, get_engine

    with get_engine().connect() as connection:
        revision = MigrationContext.configure(connection).get_current_revision()
    print(f"{_redact(database_url())}: revision {revision or '(none)'}")
    return 0


def _stats(_args) -> int:
    from db.models import ApiKey, JobIndex, User
    from db.session import session_scope

    with session_scope() as db:
        users = db.scalar(select(func.count()).select_from(User)) or 0
        admins = db.scalar(select(func.count()).select_from(User).where(User.role == "admin")) or 0
        verified = db.scalar(select(func.count()).select_from(User).where(User.email_verified_at.is_not(None))) or 0
        keys = db.scalar(select(func.count()).select_from(ApiKey).where(ApiKey.revoked_at.is_(None))) or 0
        jobs = db.scalar(select(func.count()).select_from(JobIndex)) or 0
    print(f"users: {users} ({verified} verified, {admins} admin)\nactive API keys: {keys}\nindexed jobs: {jobs}")
    return 0


def _ask_password(prompt: str = "Password: ") -> str:
    from_env = os.environ.get("HC_NEW_PASSWORD")
    if from_env:  # scripted setups (bootstrap scripts, CI)
        return from_env
    first = getpass.getpass(prompt)
    if getpass.getpass("Repeat: ") != first:
        raise SystemExit("passwords differ")
    return first


def _create_admin(args) -> int:
    from services import users

    password = _ask_password()
    with _session() as db:
        user = users.create_admin(db, args.email, password, name=args.name or "")
        db.commit()
        print(f"admin {user.email} is ready (verified)")
    return 0


def _set_role(args, role: str) -> int:
    from services import users

    with _session() as db:
        user = users.set_role(db, args.email, role)
        db.commit()
        print(f"{user.email} is now {user.role}")
    return 0


def _set_active(args, active: bool) -> int:
    from services import users

    with _session() as db:
        user = users.set_active(db, args.email, active)
        db.commit()
        print(f"{user.email} is now {'active' if user.is_active else 'deactivated'}")
    return 0


def _set_password(args) -> int:
    from services import users

    password = _ask_password("New password: ")
    with _session() as db:
        user = users.set_password_by_email(db, args.email, password)
        db.commit()
        print(f"password updated for {user.email}; their sessions were signed out")
    return 0


def _verify_email(args) -> int:
    from services import users

    with _session() as db:
        user, newly = users.mark_verified_by_email(db, args.email)
        db.commit()
        print(f"{user.email} is {'now verified' if newly else 'already verified'}")
    return 0


def _index_jobs(_args) -> int:
    from services import jobs_index

    count = jobs_index.index_orphans()
    print(f"indexed {count} job folder(s)")
    return 0


def _session():
    from db.session import get_sessionmaker

    return get_sessionmaker()()


def _redact(url: str) -> str:
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m db.cli", description="Highlight Cutter database commands")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("upgrade", help="apply migrations").set_defaults(fn=_upgrade)
    sub.add_parser("current", help="show the applied migration").set_defaults(fn=_current)
    sub.add_parser("stats", help="counts").set_defaults(fn=_stats)
    create = sub.add_parser("create-admin", help="create a verified admin user")
    create.add_argument("email")
    create.add_argument("--name", default="")
    create.set_defaults(fn=_create_admin)
    for name, fn in (("promote", lambda a: _set_role(a, "admin")), ("demote", lambda a: _set_role(a, "user")),
                     ("activate", lambda a: _set_active(a, True)), ("deactivate", lambda a: _set_active(a, False)),
                     ("set-password", _set_password), ("verify-email", _verify_email)):
        cmd = sub.add_parser(name)
        cmd.add_argument("email")
        cmd.set_defaults(fn=fn)
    sub.add_parser("index-jobs", help="index job folders the database doesn't know yet").set_defaults(fn=_index_jobs)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
