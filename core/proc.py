"""Subprocess helper for ffmpeg/ffprobe calls.

`subprocess.run(check=True)` raises a CalledProcessError whose str() is only
"Command [...] returned non-zero exit status 1." — the actual reason ffmpeg
failed lives in stderr, which was being thrown away, so a failed job said
nothing useful in `job.error`. Every media command goes through `run_checked`
so a failure carries the command and the tail of stderr.
"""

import subprocess
import threading
from collections.abc import Callable

STDERR_TAIL_BYTES = 2048


class MediaCommandError(RuntimeError):
    """An ffmpeg/ffprobe command failed or timed out. The message holds the
    command line and the last STDERR_TAIL_BYTES of its stderr."""

    def __init__(self, message: str, cmd: list[str], stderr_tail: str = "", returncode: int | None = None):
        super().__init__(message)
        self.cmd = cmd
        self.stderr_tail = stderr_tail
        self.returncode = returncode


def _tail(text: str | bytes | None, limit: int = STDERR_TAIL_BYTES) -> str:
    if not text:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = text.strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def format_cmd(cmd: list[str], limit: int = 1500) -> str:
    joined = " ".join(str(c) for c in cmd)
    if len(joined) <= limit:
        return joined
    return joined[:limit] + f" … (+{len(joined) - limit} chars)"


# Optional hook the job runner installs so long subprocesses can bump the job's
# heartbeat and honour cancellation (see api/queue.py). Thread-local because the
# worker thread is the only one that should be affected.
_hooks = threading.local()


def set_poll_hook(hook: Callable[[], None] | None, interval: float = 30.0) -> None:
    """Install a callable that is invoked roughly every `interval` seconds
    while a subprocess runs on this thread. It may raise to abort the command."""
    _hooks.poll = hook
    _hooks.interval = interval


def _poll_hook() -> Callable[[], None] | None:
    return getattr(_hooks, "poll", None)


def run_checked(
    cmd: list[str],
    timeout: float | None = None,
    *,
    cwd: str | None = None,
    poll_interval: float | None = None,
) -> subprocess.CompletedProcess:
    """Run a media command, capturing text output. Raises MediaCommandError on a
    non-zero exit or a timeout, with the command and the stderr tail in the
    message."""
    cmd = [str(c) for c in cmd]
    hook = _poll_hook()
    if poll_interval is None:
        poll_interval = getattr(_hooks, "interval", 30.0)
    try:
        if hook is None:
            return subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=timeout, cwd=cwd,
                encoding="utf-8", errors="replace",
            )
        return _run_with_hook(cmd, timeout, cwd, poll_interval, hook)
    except subprocess.CalledProcessError as exc:
        tail = _tail(exc.stderr)
        raise MediaCommandError(
            f"{_name(cmd)} failed (exit {exc.returncode}): {format_cmd(cmd)}\n--- stderr (tail) ---\n{tail}",
            cmd, tail, exc.returncode,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        tail = _tail(exc.stderr)
        raise MediaCommandError(
            f"{_name(cmd)} timed out after {timeout:.0f}s: {format_cmd(cmd)}\n--- stderr (tail) ---\n{tail}",
            cmd, tail, None,
        ) from exc


def _name(cmd: list[str]) -> str:
    exe = cmd[0].replace("\\", "/").rsplit("/", 1)[-1] if cmd else "command"
    return exe.removesuffix(".exe")


def _run_with_hook(
    cmd: list[str], timeout: float | None, cwd: str | None, poll_interval: float, hook: Callable[[], None]
) -> subprocess.CompletedProcess:
    """Popen + communicate in slices so the hook runs periodically (heartbeat /
    cancellation) without an extra thread per command."""
    import time

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd,
        encoding="utf-8", errors="replace",
    )
    deadline = None if timeout is None else time.monotonic() + timeout
    out_parts: list[str] = []
    err_parts: list[str] = []
    try:
        while True:
            wait = poll_interval
            if deadline is not None:
                wait = min(wait, max(0.0, deadline - time.monotonic()))
            try:
                out, err = proc.communicate(timeout=wait)
                out_parts.append(out or "")
                err_parts.append(err or "")
                break
            except subprocess.TimeoutExpired:
                if deadline is not None and time.monotonic() >= deadline:
                    proc.kill()
                    out, err = proc.communicate()
                    raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err) from None
                hook()  # may raise (e.g. cancellation) -> finally kills the process
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    stdout, stderr = "".join(out_parts), "".join(err_parts)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
