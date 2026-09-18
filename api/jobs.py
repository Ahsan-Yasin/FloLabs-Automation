import json
import os
import threading

from core.config import get_settings
from core.models import JobRecord


class JobStore:
    """Simple thread-safe job store: JSON-on-disk with an in-memory cache.

    Good enough for a single-process batch service called by n8n (section 6);
    swap for a real DB/queue if this needs to scale beyond one worker.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, JobRecord] = {}

    def _path(self, job_id: str):
        return get_settings().jobs_dir / job_id / "job.json"

    def create(self, job: JobRecord) -> None:
        with self._lock:
            self._cache[job.job_id] = job
            self._persist(job)

    def update(self, job: JobRecord) -> None:
        with self._lock:
            self._cache[job.job_id] = job
            self._persist(job)

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            if job_id in self._cache:
                return self._cache[job_id]
        path = self._path(job_id)
        if not path.exists():
            return None
        job = JobRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
        with self._lock:
            self._cache[job_id] = job
        return job

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
