"""Relational store for the product layer: users, sessions, email tokens, API
keys, the jobs index (ownership) and webhook deliveries (plan.MD §4).

Job state itself stays in storage/jobs/{id}/job.json (api/jobs.JobStore);
the database only indexes jobs so they can be listed per user.
"""

from .base import Base
from .session import (
    database_url,
    dispose_all,
    get_engine,
    get_sessionmaker,
    session_scope,
)

__all__ = ["Base", "database_url", "dispose_all", "get_engine", "get_sessionmaker", "session_scope"]
