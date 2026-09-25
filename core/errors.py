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


_DISK_FULL_MARKERS = ("no space left on device", "not enough space on the disk", "disk full")


def is_disk_full(exc: BaseException) -> bool:
    """A full disk, however it surfaced: our own preflight, an OSError from a
    Python write (ENOSPC; Windows ERROR_DISK_FULL / ERROR_HANDLE_DISK_FULL),
    or ffmpeg failing with "No space left on device"."""
    import errno

    from core.proc import MediaCommandError

    if isinstance(exc, InsufficientDisk):
        return True
    if isinstance(exc, OSError):
        return exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112)
    if isinstance(exc, MediaCommandError):
        return any(m in (exc.stderr_tail or "").lower() for m in _DISK_FULL_MARKERS)
    return False


def is_fatal(exc: BaseException) -> bool:
    """Errors that must end the job even inside an optional output (plan D18):
    a cancel, the job's wall clock, a full disk (retrying later can work;
    silently dropping an output would not)."""
    return isinstance(exc, (JobCancelled, JobTimedOut)) or is_disk_full(exc)


def classify(exc: BaseException) -> tuple[str, bool, int | None]:
    """(error_code, retryable, retry_after_s) for any exception a job raised."""
    from core.proc import MediaCommandError

    if is_disk_full(exc):
        return "insufficient_disk", True, None
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
        # a bad API key or model name can't be fixed by retrying
        return "llm_error", bool(getattr(exc, "retryable", True)), None
    return "internal", False, None
