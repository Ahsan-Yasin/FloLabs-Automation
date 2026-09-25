"""Single-worker job queue, heartbeat, cancellation and startup reconciliation
(plan D15).

Why a dedicated worker instead of FastAPI BackgroundTasks: sync background
tasks run in a 40-thread pool with no ordering, so two jobs would render at
once and fight for RAM/CPU, and a stopped instance left job.json "running"
forever. Here exactly one job runs at a time, a heartbeat keeps `updated_at`
fresh while ffmpeg works, DELETE can cancel a running job cooperatively, and
anything left non-terminal by a restart is marked failed/interrupted.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.config import get_settings
from core.errors import JobCancelled, JobTimedOut
from core.logging import get_logger
from core.models import TERMINAL_STATUSES, JobRecord, JobStatus
from core.proc import set_poll_hook

from .jobs import JobStore, is_valid_job_id

logger = get_logger(__name__)

KEEP_MARKER = ".keep"


class QueueFull(Exception):
    def __init__(self, running: str | None, depth: int):
        super().__init__(f"busy: {depth} job(s) in the system (running: {running})")
        self.running = running
        self.depth = depth


class JobQueue:
    def __init__(self, store: JobStore) -> None:
        self._store = store
        self._cv = threading.Condition()
        self._pending: deque[tuple[str, Callable[[], None]]] = deque()
        self._running: str | None = None
        self._running_started: float = 0.0
        self._cancel: set[str] = set()
        self._delete_after: set[str] = set()
        self._thread: threading.Thread | None = None
        self._idle_since = time.monotonic()

    # ------------------------------------------------------------- state
    @property
    def running_job_id(self) -> str | None:
        return self._running

    def pending_ids(self) -> list[str]:
        with self._cv:
            return [job_id for job_id, _ in self._pending]

    def depth(self) -> int:
        with self._cv:
            return len(self._pending) + (1 if self._running else 0)

    def is_busy(self) -> bool:
        return self.depth() > 0

    def contains(self, job_id: str) -> bool:
        with self._cv:
            return job_id == self._running or any(j == job_id for j, _ in self._pending)

    # ------------------------------------------------------------- submit
    def submit(self, job_id: str, fn: Callable[[], None], max_depth: int | None = None) -> None:
        max_depth = get_settings().max_queue_depth if max_depth is None else max_depth
        with self._cv:
            depth = len(self._pending) + (1 if self._running else 0)
            if max_depth > 0 and depth >= max_depth:
                raise QueueFull(self._running, depth)
            self._pending.append((job_id, fn))
            self._ensure_worker()
            self._cv.notify_all()

    def _ensure_worker(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, name="job-worker", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------- cancel
    def cancel(self, job_id: str, delete_after: bool = False) -> str:
        """'dequeued' (was waiting, now removed), 'signalled' (running; it will
        stop at its next checkpoint), or 'not_found'."""
        with self._cv:
            for i, (queued_id, _) in enumerate(self._pending):
                if queued_id == job_id:
                    del self._pending[i]
                    return "dequeued"
            if self._running == job_id:
                self._cancel.add(job_id)
                if delete_after:
                    self._delete_after.add(job_id)
                return "signalled"
        return "not_found"

    def check(self, job_id: str) -> None:
        """Raise if the running job was cancelled or ran past job_max_wall_s.
        Called from progress updates and from the ffmpeg heartbeat."""
        if job_id in self._cancel:
            raise JobCancelled("cancelled by DELETE /jobs/{id}?force=true")
        if self._running == job_id:
            limit = get_settings().job_max_wall_s
            if limit and time.monotonic() - self._running_started > limit:
                raise JobTimedOut(f"job exceeded job_max_wall_s={limit:.0f}s")

    def make_updater(self, job_id: str) -> Callable[[JobRecord], None]:
        """The `update` callback handed to the pipeline: persists the job and,
        while it is still running, is a cancellation/timeout checkpoint."""

        def update(job: JobRecord) -> None:
            self._store.update(job)
            if job.status not in TERMINAL_STATUSES:
                self.check(job_id)

        return update

    # ------------------------------------------------------------- worker
    def _worker(self) -> None:
        while True:
            with self._cv:
                while not self._pending:
                    self._cv.wait()
                job_id, fn = self._pending.popleft()
                self._running = job_id
                self._running_started = time.monotonic()
            settings = get_settings()
            handler = _attach_job_log(job_id)
            set_poll_hook(lambda jid=job_id: self._heartbeat(jid), settings.heartbeat_interval_s)
            try:
                fn()
            except BaseException:
                logger.exception("job %s crashed outside the pipeline's own error handling", job_id)
                self._mark_crashed(job_id)
            finally:
                set_poll_hook(None)
                _detach_job_log(handler)  # close job.log first: Windows can't delete open files
                with self._cv:
                    delete = job_id in self._delete_after
                    self._delete_after.discard(job_id)
                if delete:
                    delete_job_files(self._store, job_id)
                with self._cv:
                    self._running = None
                    self._cancel.discard(job_id)
                    self._idle_since = time.monotonic()
                    self._cv.notify_all()

    def _heartbeat(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job is not None and job.status not in TERMINAL_STATUSES:
            self._store.update(job)  # bumps updated_at
        self.check(job_id)

    def _mark_crashed(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job is not None and job.status not in TERMINAL_STATUSES:
            job.status = JobStatus.FAILED
            job.error_code = "internal"
            job.error = job.error or "the job crashed unexpectedly (see job.log)"
            self._store.update(job)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Test helper: block until nothing is queued or running."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._pending or self._running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
        return True


# ----------------------------------------------------------------- job.log

class _ThreadFilter(logging.Filter):
    def __init__(self, thread_id: int) -> None:
        super().__init__()
        self.thread_id = thread_id

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread == self.thread_id


def _attach_job_log(job_id: str) -> logging.Handler | None:
    try:
        path = get_settings().jobs_dir / job_id / "job.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        return None
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_ThreadFilter(threading.get_ident()))
    logging.getLogger().addHandler(handler)
    return handler


def _detach_job_log(handler: logging.Handler | None) -> None:
    if handler is None:
        return
    logging.getLogger().removeHandler(handler)
    handler.close()


# ----------------------------------------------------------------- files

def is_protected(job_id: str) -> bool:
    return (get_settings().jobs_dir / job_id / KEEP_MARKER).exists()


def delete_job_files(store: JobStore, job_id: str) -> None:
    """Remove everything the job owns: its folder, a YouTube download it left
    in videos/ (see _owned_download), plus a legacy upload stored as
    videos/{job_id}.* before sources moved into the job folder.

    Refuses ids that are not plain job ids (on Windows "<id>." names a running
    job's folder, and "..\\" climbs out of jobs/) and `.keep`-protected jobs:
    every caller already checks protection, but this is the function that
    actually deletes, so the regression fixture must be safe here too."""
    settings = get_settings()
    if not is_valid_job_id(job_id):
        logger.warning("refusing to delete files for invalid job id %r", job_id)
        return
    if is_protected(job_id):
        logger.warning("refusing to delete protected job %s (it has a %s marker)", job_id, KEEP_MARKER)
        return
    download = _owned_download(store, job_id)  # read job.json before forget() and rmtree
    store.forget(job_id)
    shutil.rmtree(settings.jobs_dir / job_id, ignore_errors=True)
    for legacy in settings.videos_dir.glob(f"{job_id}.*"):
        legacy.unlink(missing_ok=True)
    if download is not None:
        download.unlink(missing_ok=True)


def _owned_download(store: JobStore, job_id: str) -> Path | None:
    """The job's source file when it sits directly in videos/ under a name the
    legacy {job_id}.* glob can't match: download_youtube(url) saves to
    videos/<random>.<ext>, so without this every YouTube job leaked its
    download past DELETE and the retention sweep.

    Only a file directly inside videos/ is ever returned (never anything
    elsewhere, e.g. a source the caller pointed at by path), and not one that
    another job still names as its source (or when that can't be checked
    because a job.json is unreadable): a shared file isn't this job's to
    delete."""
    try:
        job = store.get(job_id)
    except Exception as exc:  # noqa: BLE001 — an unreadable job.json must not block deleting its folder
        logger.warning("job %s: can't read job.json, leaving its source alone: %s", job_id, exc)
        return None
    if job is None or not job.source_path:
        return None
    source = Path(job.source_path).resolve()
    if source.parent != get_settings().videos_dir.resolve() or not source.is_file():
        return None
    for other_id in store.list_ids():
        if other_id == job_id:
            continue
        try:
            other = store.get(other_id)
        except Exception as exc:  # noqa: BLE001 — unknown owner: keep the file rather than guess
            logger.warning("job %s: can't read job %s (%s), leaving %s alone", job_id, other_id, exc, source)
            return None
        if other is not None and other.source_path and Path(other.source_path).resolve() == source:
            return None
    return source


# ----------------------------------------------------------------- startup

def reconcile_on_startup(store: JobStore) -> dict[str, int]:
    """Anything a previous process left non-terminal can never finish: mark it
    failed/interrupted (retryable) and drop its temp files. Then apply the
    retention policy (0 = keep everything)."""
    settings = get_settings()
    interrupted = swept = 0
    for job_id in store.list_ids():
        path = settings.jobs_dir / job_id / "job.json"
        try:
            job = JobRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 — one bad file must not stop startup
            logger.warning("skipping unreadable %s: %s", path, exc)
            continue
        if job.created_at is None:  # written before timestamps existed
            job.created_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if job.status not in TERMINAL_STATUSES:
            previous = job.status.value
            job.status = JobStatus.FAILED
            job.error_code = "interrupted"
            job.retryable = True
            job.error = f"the service stopped while this job was {previous}; submit it again"
            store.update(job)
            shutil.rmtree(settings.jobs_dir / job_id / "tmp", ignore_errors=True)
            interrupted += 1
            continue
        if settings.job_retention_hours > 0 and not is_protected(job_id):
            stamp = job.updated_at or job.created_at
            if stamp is not None:
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                if datetime.now(UTC) - stamp > timedelta(hours=settings.job_retention_hours):
                    delete_job_files(store, job_id)
                    swept += 1
    if interrupted or swept:
        logger.info("startup reconciliation: %d interrupted, %d deleted by retention", interrupted, swept)
    return {"interrupted": interrupted, "deleted": swept}
