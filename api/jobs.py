import json
import os
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from core.config import get_settings
from core.logging import get_logger
from core.models import JobRecord

logger = get_logger(__name__)

# Job ids are uuid4 hex (or short test/fixture names). Anything else must never
# be joined onto jobs_dir: on Windows "<id>." and "<id> " open the same folder
# as "<id>" (so a lookup would find a running job under a name the queue does
# not know), and a URL-decoded "\" is a path separator.
_JOB_ID_RE = re.compile(r"[0-9a-zA-Z_-]{1,64}")


def is_valid_job_id(job_id: str) -> bool:
    return _JOB_ID_RE.fullmatch(job_id) is not None


class JobStore:
    """Simple thread-safe job store: JSON-on-disk with an in-memory cache.

    Good enough for a single-process batch service called by n8n (section 6);
    swap for a real DB/queue if this needs to scale beyond one worker.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, JobRecord] = {}
        # Jobs whose folder was deleted: late updates from a finishing worker
        # must not recreate the folder.
        self._deleted: set[str] = set()
        # Product-layer hooks (api/main.py wires them to services.jobs_index):
        # called after every save / when a job is deleted, outside the lock.
        # They must not raise; errors are logged and ignored here too.
        self.on_change: Callable[[JobRecord], None] | None = None
        self.on_delete: Callable[[str], None] | None = None

    def _path(self, job_id: str):
        return get_settings().jobs_dir / job_id / "job.json"

    def create(self, job: JobRecord) -> None:
        with self._lock:
            now = datetime.now(UTC)
            if job.created_at is None:
                job.created_at = now
            job.updated_at = now
            self._deleted.discard(job.job_id)
            self._cache[job.job_id] = job
            self._persist(job)
        self._notify(job)

    def update(self, job: JobRecord) -> None:
        with self._lock:
            if job.job_id in self._deleted:
                return
            job.updated_at = datetime.now(UTC)
            if job.created_at is None:
                job.created_at = job.updated_at
            self._cache[job.job_id] = job
            self._persist(job)
        self._notify(job)

    def get(self, job_id: str) -> JobRecord | None:
        if not is_valid_job_id(job_id):
            return None
        with self._lock:
            if job_id in self._deleted:
                return None
            if job_id in self._cache:
                return self._cache[job_id]
        path = self._path(job_id)
        if not path.exists():
            return None
        job = JobRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
        with self._lock:
            self._cache[job_id] = job
        return job

    def forget(self, job_id: str) -> None:
        """Drop a job from the cache and ignore any later updates to it (its
        folder is being deleted)."""
        with self._lock:
            self._cache.pop(job_id, None)
            self._deleted.add(job_id)
        if self.on_delete is not None:
            try:
                self.on_delete(job_id)
            except Exception:  # bookkeeping must never block a delete
                logger.exception("job store: on_delete hook failed for %s", job_id)

    def list_ids(self) -> list[str]:
        jobs_dir = get_settings().jobs_dir
        if not jobs_dir.exists():
            return []
        with self._lock:
            deleted = set(self._deleted)
        return [p.parent.name for p in jobs_dir.glob("*/job.json") if p.parent.name not in deleted]

    def _notify(self, job: JobRecord) -> None:
        if self.on_change is not None:
            try:
                self.on_change(job)
            except Exception:  # bookkeeping must never fail a job
                logger.exception("job store: on_change hook failed for %s", job.job_id)

    def _persist(self, job: JobRecord) -> None:
        path = self._path(job.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A job.json is rewritten many times per job (once per pipeline stage,
        # see pipeline.py's `update(job)` calls), and a straight write_text()
        # truncates the file before writing the new bytes. A reader (a GET
        # /jobs/{id} landing in the disk-fallback path of get(), e.g. right
        # after a process restart) that opens the file in that window — or a
        # process killed mid-write — sees/leaves a truncated, non-JSON file.
        # Once that happens, every future get() for that job permanently
        # raises JSONDecodeError, since nothing ever rewrites a "corrupt" file
        # with a fresh full one. Write to a temp file and rename atomically so
        # a reader never observes a partial write.
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(job.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp_path, path)


job_store = JobStore()
