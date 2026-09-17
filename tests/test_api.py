import io
import time

from fastapi.testclient import TestClient

import api.main as api_main
from core.models import JobStatus


def _fake_run_pipeline(job, update):
    job.status = JobStatus.DONE
    update(job)


def test_create_and_fetch_job(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _fake_run_pipeline)
    client = TestClient(api_main.app)

    response = client.post(
        "/jobs",
        files={"file": ("meeting.mp4", io.BytesIO(b"fake bytes"), "video/mp4")},
    )
    assert response.status_code == 200
    job = response.json()
    assert job["status"] in (JobStatus.QUEUED.value, JobStatus.DONE.value)

    # the background task may finish just after the response is sent
    fetched = None
    for _ in range(50):
        fetched = client.get(f"/jobs/{job['job_id']}")
        if fetched.json()["status"] == JobStatus.DONE.value:
            break
        time.sleep(0.05)

    assert fetched.status_code == 200
    assert fetched.json()["job_id"] == job["job_id"]
    assert fetched.json()["status"] == JobStatus.DONE.value


def test_rejects_unsupported_file_type(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _fake_run_pipeline)
    client = TestClient(api_main.app)

    response = client.post(
        "/jobs",
        files={"file": ("meeting.avi", io.BytesIO(b"fake bytes"), "video/avi")},
    )
    assert response.status_code == 400


def test_unknown_job_returns_404():
    client = TestClient(api_main.app)
    response = client.get("/jobs/does-not-exist")
    assert response.status_code == 404
