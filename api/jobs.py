import json
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
        path.write_text(job.model_dump_json(indent=2), encoding="utf-8")


job_store = JobStore()
