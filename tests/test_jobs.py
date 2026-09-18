import threading

from api.jobs import JobStore
from core.models import JobRecord, JobStatus


def _job(job_id="job-1", status=JobStatus.QUEUED, **kwargs) -> JobRecord:
    return JobRecord(job_id=job_id, status=status, source_path="fake.mp4", **kwargs)


def test_get_falls_back_to_disk_when_not_cached():
    """Simulates a process restart: a fresh JobStore (empty in-memory cache)
    must still be able to look up a job that a previous instance persisted."""
    store_a = JobStore()
    store_a.create(_job(status=JobStatus.QUEUED))

    store_b = JobStore()  # fresh cache, same underlying storage_dir
    fetched = store_b.get("job-1")

    assert fetched is not None
    assert fetched.job_id == "job-1"
    assert fetched.status == JobStatus.QUEUED


def test_get_returns_none_for_unknown_job():
    store = JobStore()
    assert store.get("does-not-exist") is None


def test_persist_is_atomic_no_leftover_tmp_file():
    """A crash mid-write must never leave readers looking at a half-written
    job.json. Writing succeeds via a temp file + atomic rename, so no `.tmp`
    file should remain after a normal update, and the on-disk file must always
    be one complete, parseable JSON document."""
    store = JobStore()
    job = _job()
    store.create(job)

    job.status = JobStatus.DONE
    store.update(job)

    job_dir = store._path(job.job_id).parent
    files = sorted(p.name for p in job_dir.iterdir())
    assert files == ["job.json"]

    # A second store (disk-only read path) must see the fully-written update.
    reread = JobStore().get(job.job_id)
    assert reread.status == JobStatus.DONE


def test_concurrent_updates_do_not_corrupt_or_lose_the_record():
    """Many rapid status updates from concurrent threads (mirrors the real
    pipeline calling `update(job)` after every stage while a request thread
    might be reading the same job) must never leave job.json truncated or
    unparsable, and the last write must win cleanly."""
    store = JobStore()
    job = _job()
    store.create(job)

    statuses = [
        JobStatus.DOWNLOADING,
        JobStatus.TRANSCRIBING,
        JobStatus.DECIDING,
        JobStatus.BUILDING_EDL,
        JobStatus.SLICING,
        JobStatus.DONE,
    ]
    errors = []

    def writer(status):
        try:
            j = _job(status=status)
            store.update(j)
        except Exception as exc:  # noqa: BLE001 — collecting thread failures, not swallowing them
            errors.append(exc)

    def reader():
        try:
            for _ in range(50):
                got = store.get(job.job_id)
                assert got is not None
        except Exception as exc:  # noqa: BLE001 — collecting thread failures, not swallowing them
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(s,)) for s in statuses]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    final = JobStore().get(job.job_id)
    assert final is not None
    assert final.status in statuses
