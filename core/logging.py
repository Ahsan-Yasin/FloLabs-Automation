"""Logging for the service: plain lines locally, one JSON object per line in
production (LOG_JSON=true), always tagged with the request id the security
middleware gives every request (X-Request-ID) so a user's report can be
matched to the server's log."""

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

# set by api.security.SecurityHeadersMiddleware for the duration of a request;
# "-" outside requests (the job worker, startup)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_PLAIN_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s"


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: what log collectors (CloudWatch, Loki,
    `docker compose logs | jq`) parse without regexes."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(level: int = logging.INFO, *, json_lines: bool | None = None) -> None:
    """Idempotent: reconfigures the root handler each call (tests and the CLI
    call it more than once)."""
    if json_lines is None:
        from core.config import get_settings

        json_lines = get_settings().log_json
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(JsonFormatter() if json_lines else logging.Formatter(_PLAIN_FORMAT))
    root = logging.getLogger()
    for old in list(root.handlers):
        if getattr(old, "_highlight_cutter", False):
            root.removeHandler(old)
    handler._highlight_cutter = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
