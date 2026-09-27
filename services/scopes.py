"""What an API key may do (plan.MD §5.3). Browser sessions, bearer access
tokens and the operator key have every scope."""

JOBS_READ = "jobs:read"
JOBS_WRITE = "jobs:write"

SCOPE_DESCRIPTIONS = {
    JOBS_READ: "List jobs, read their status and download their outputs",
    JOBS_WRITE: "Create, render, cancel and delete jobs",
}

ALL_SCOPES = frozenset(SCOPE_DESCRIPTIONS)
