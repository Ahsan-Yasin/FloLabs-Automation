"""Exceptions that carry the machine-readable job error code (plan §11)."""

from __future__ import annotations


class PipelineError(RuntimeError):
    """A job failure with a known `error_code` and retry hint."""

    code: str = "internal"
    retryable: bool = False

    def __init__(self, message: str, *, code: str | None = None, retryable: bool | None = None,
                 retry_after_s: int | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.retry_after_s = retry_after_s


class InsufficientDisk(PipelineError):
    code = "insufficient_disk"
    retryable = True


class RenderAssertError(PipelineError):
    """A rendered file does not match what the EDL says it should contain."""

    code = "render_assert_failed"


class SourceUnsupported(PipelineError):
    code = "source_unsupported"


class JobCancelled(PipelineError):
    code = "cancelled"


class JobTimedOut(PipelineError):
    code = "timeout"
    retryable = True


def classify(exc: BaseException) -> tuple[str, bool, int | None]:
    """(error_code, retryable, retry_after_s) for any exception a job raised."""
    from core.proc import MediaCommandError

    if isinstance(exc, PipelineError):
        return exc.code, exc.retryable, exc.retry_after_s
    if isinstance(exc, MediaCommandError):
        if "timed out" in str(exc):
            return "timeout", True, None
        return "internal", False, None
    try:
        from decide.gemini_client import DecisionError
    except Exception:  # noqa: BLE001 — optional import, never mask the real error
        DecisionError = None  # type: ignore[assignment]
    if DecisionError is not None and isinstance(exc, DecisionError):
        return "llm_error", True, None
    return "internal", False, None
