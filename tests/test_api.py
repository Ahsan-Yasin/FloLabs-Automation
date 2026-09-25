import io
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
import pipeline as pipeline_module
from api.jobs import job_store
from api.queue import reconcile_on_startup
from core.config import get_settings
from core.models import JobStatus


@pytest.fixture(autouse=True)
def _queue_drained():
    yield
    assert api_main.job_queue.wait_idle(10), "a test left a job running"


def _fake_run_pipeline(job, update):
    job.status = JobStatus.DONE
    update(job)


def _fake_run_youtube_pipeline(job, update, url):
    job.status = JobStatus.DONE
    job.source_path = "/tmp/fake.mp4"
    update(job)


def _wait_until_done(client, job_id):
    fetched = None
    for _ in range(50):
        fetched = client.get(f"/jobs/{job_id}")
        if fetched.json()["status"] in (JobStatus.DONE.value, JobStatus.FAILED.value):
            break
        time.sleep(0.05)
    return fetched


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


def test_create_youtube_job(monkeypatch):
    monkeypatch.setattr(api_main, "run_youtube_pipeline", _fake_run_youtube_pipeline)
    client = TestClient(api_main.app)

    response = client.post(
        "/jobs/youtube", json={"url": "https://youtube.com/watch?v=abc", "options": {"shorts_count": 2}}
    )
    assert response.status_code == 200
    job = response.json()
    assert job["options"]["shorts_count"] == 2
    assert job["source_url"] == "https://youtube.com/watch?v=abc"

    fetched = _wait_until_done(client, job["job_id"])
    assert fetched.json()["status"] == JobStatus.DONE.value


def test_youtube_job_rejects_empty_url(monkeypatch):
    monkeypatch.setattr(api_main, "run_youtube_pipeline", _fake_run_youtube_pipeline)
    client = TestClient(api_main.app)

    response = client.post("/jobs/youtube", json={"url": "   "})
    assert response.status_code == 400


def test_index_serves_html():
    client = TestClient(api_main.app)
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Highlight Cutter" in response.text


def test_health_needs_no_job_and_reports_basics():
    client = TestClient(api_main.app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["disk_free_bytes"] is None or body["disk_free_bytes"] > 0
    assert body["uptime_s"] >= 0


# ---------------------------------------------------------------- v2 service


def _upload(client):
    return client.post("/jobs", files={"file": ("meeting.mp4", io.BytesIO(b"fake bytes"), "video/mp4")})


def _blocking_runner(started: threading.Event, release: threading.Event):
    def run(job, update):
        try:
            job.status = JobStatus.TRANSCRIBING
            update(job)
            started.set()
            while not release.wait(0.02):
                update(job)  # a checkpoint, like the real pipeline's progress updates
            job.status = JobStatus.DONE
            update(job)
        except Exception as exc:  # noqa: BLE001 — mirror run_pipeline's own handler
            pipeline_module._fail(job, update, exc)

    return run


def test_api_key_required_when_configured(monkeypatch):
    monkeypatch.setenv("HC_API_TOKEN", "s3cret")
    get_settings.cache_clear()
    client = TestClient(api_main.app)
    assert client.get("/jobs/nope").status_code == 401
    assert client.get("/jobs/nope", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/jobs/nope", headers={"X-API-Key": "s3cret"}).status_code == 404
    client.cookies.set("hc_api_key", "s3cret")
    assert client.get("/jobs/nope").status_code == 404
    anon = TestClient(api_main.app)
    assert anon.get("/health").status_code == 200
    assert anon.get("/").status_code == 200
    assert anon.get("/health").json()["auth_enabled"] is True


def test_second_job_while_busy_gets_503(monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(api_main, "run_pipeline", _blocking_runner(started, release))
    client = TestClient(api_main.app)
    first = _upload(client)
    assert first.status_code == 200
    assert started.wait(5)
    second = _upload(client)
    assert second.status_code == 503
    body = second.json()
    assert body["error_code"] == "busy" and body["retryable"] is True
    assert body["running_job_id"] == first.json()["job_id"]
    assert second.headers["Retry-After"]
    assert client.get("/health").json()["busy"] is True
    release.set()


def test_delete_running_job_needs_force_then_cancels(monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(api_main, "run_pipeline", _blocking_runner(started, release))
    client = TestClient(api_main.app)
    job_id = _upload(client).json()["job_id"]
    assert started.wait(5)
    job_dir = get_settings().jobs_dir / job_id

    refused = client.delete(f"/jobs/{job_id}")
    assert refused.status_code == 409
    accepted = client.delete(f"/jobs/{job_id}?force=true")
    assert accepted.status_code == 202
    assert api_main.job_queue.wait_idle(10)
    assert not job_dir.exists()
    assert client.get(f"/jobs/{job_id}").status_code == 404


def test_cancelled_job_reports_cancelled_status(monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(api_main, "run_pipeline", _blocking_runner(started, release))
    client = TestClient(api_main.app)
    job_id = _upload(client).json()["job_id"]
    assert started.wait(5)
    job = job_store.get(job_id)
    api_main.job_queue.cancel(job_id)  # cancel without deleting
    assert api_main.job_queue.wait_idle(10)
    assert job.status == JobStatus.CANCELLED
    assert job.error_code == "cancelled"


def test_cancel_kills_a_long_running_subprocess_via_heartbeat(monkeypatch):
    """DELETE ?force=true must stop a job even while ffmpeg is mid-command:
    the heartbeat hook runs during the subprocess and raises JobCancelled."""
    import sys

    from core.proc import run_checked

    monkeypatch.setenv("HEARTBEAT_INTERVAL_S", "0.1")
    get_settings.cache_clear()
    started = threading.Event()

    def runner(job, update):
        try:
            job.status = JobStatus.SLICING
            update(job)
            started.set()
            run_checked([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60)
            job.status = JobStatus.DONE
            update(job)
        except Exception as exc:  # noqa: BLE001 — mirror run_pipeline's own handler
            pipeline_module._fail(job, update, exc)

    monkeypatch.setattr(api_main, "run_pipeline", runner)
    client = TestClient(api_main.app)
    job_id = _upload(client).json()["job_id"]
    job = job_store.get(job_id)
    assert started.wait(5)
    t0 = time.monotonic()
    api_main.job_queue.cancel(job_id)
    assert api_main.job_queue.wait_idle(10)
    assert time.monotonic() - t0 < 5  # not the 30 s the command wanted
    assert job.status == JobStatus.CANCELLED


def test_delete_queued_and_finished_jobs(monkeypatch):
    monkeypatch.setenv("MAX_QUEUE_DEPTH", "2")
    get_settings.cache_clear()
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(api_main, "run_pipeline", _blocking_runner(started, release))
    client = TestClient(api_main.app)
    running = _upload(client).json()["job_id"]
    assert started.wait(5)
    queued = _upload(client).json()["job_id"]
    assert client.delete(f"/jobs/{queued}").json() == {"job_id": queued, "deleted": True}
    assert not (get_settings().jobs_dir / queued).exists()
    release.set()
    assert api_main.job_queue.wait_idle(10)
    assert client.get(f"/jobs/{running}").json()["status"] == "done"
    assert client.delete(f"/jobs/{running}").status_code == 200
    assert not (get_settings().jobs_dir / running).exists()


def test_protected_job_cannot_be_deleted(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _fake_run_pipeline)
    client = TestClient(api_main.app)
    job_id = _upload(client).json()["job_id"]
    assert api_main.job_queue.wait_idle(10)
    (get_settings().jobs_dir / job_id / ".keep").write_text("")
    assert client.delete(f"/jobs/{job_id}").status_code == 409
    assert (get_settings().jobs_dir / job_id).exists()


def test_job_list_and_log(monkeypatch):
    def logging_runner(job, update):
        logging.getLogger("test.job").warning("hello from inside the job")
        job.status = JobStatus.DONE
        update(job)

    monkeypatch.setattr(api_main, "run_pipeline", logging_runner)
    client = TestClient(api_main.app)
    job_id = _upload(client).json()["job_id"]
    assert api_main.job_queue.wait_idle(10)
    listed = client.get("/jobs").json()
    assert listed[0]["job_id"] == job_id and listed[0]["status"] == "done"
    assert "hello from inside the job" in client.get(f"/jobs/{job_id}/log").text


def _write_job(job_id, **fields):
    job_dir = get_settings().jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    data = {"job_id": job_id, "status": "queued", "source_path": "x.mp4", **fields}
    (job_dir / "job.json").write_text(json.dumps(data), encoding="utf-8")
    return job_dir


def test_startup_marks_unfinished_jobs_interrupted():
    job_dir = _write_job("stuck-job", status="slicing")
    (job_dir / "tmp").mkdir()
    (job_dir / "tmp" / "piece.mp4").write_bytes(b"x")
    _write_job("finished-job", status="done")
    result = reconcile_on_startup(job_store)
    assert result["interrupted"] == 1
    stuck = job_store.get("stuck-job")
    assert stuck.status == JobStatus.FAILED
    assert stuck.error_code == "interrupted" and stuck.retryable is True
    assert not (job_dir / "tmp").exists()
    assert job_store.get("finished-job").status == JobStatus.DONE


def test_retention_deletes_old_jobs_but_honours_keep(monkeypatch):
    monkeypatch.setenv("JOB_RETENTION_HOURS", "1")
    get_settings.cache_clear()
    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    _write_job("old-job", status="done", updated_at=old)
    kept_dir = _write_job("kept-job", status="done", updated_at=old)
    (kept_dir / ".keep").write_text("")
    _write_job("new-job", status="done", updated_at=datetime.now(UTC).isoformat())
    result = reconcile_on_startup(job_store)
    assert result["deleted"] == 1
    assert not (get_settings().jobs_dir / "old-job").exists()
    assert kept_dir.exists() and (get_settings().jobs_dir / "new-job").exists()


def test_stale_flag_when_heartbeat_stops():
    _write_job("quiet-job", status="deciding",
               updated_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    client = TestClient(api_main.app)
    assert client.get("/jobs/quiet-job").json()["stale"] is True


# ---------------------------------------------------------------- v2 decide options


def test_upload_accepts_job_options(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _fake_run_pipeline)
    client = TestClient(api_main.app)
    response = client.post(
        "/jobs",
        files={"file": ("meeting.mp4", io.BytesIO(b"fake bytes"), "video/mp4")},
        data={"decide_only": "true", "shorts_count": "2", "highlights_criteria": "  jokes about coffee "},
    )
    assert response.status_code == 200
    options = response.json()["options"]
    assert options["decide_only"] is True and options["shorts_count"] == 2
    assert options["highlights_criteria"] == "jokes about coffee"
    bad = client.post("/jobs", files={"file": ("m.mp4", io.BytesIO(b"x"), "video/mp4")}, data={"shorts_count": "99"})
    assert bad.status_code == 422


def _decided_runner(job, update):
    job_dir = get_settings().jobs_dir / job.job_id
    (job_dir / "selection.json").write_text(json.dumps({"moments": [], "highlights": {}, "shorts": []}))
    job.selection_path = str(job_dir / "selection.json")
    job.status = JobStatus.DECIDED if job.options.decide_only else JobStatus.DONE
    if job.status == JobStatus.DONE:
        (job_dir / "chapters.txt").write_text("00:00 Intro\n01:00 Plans\n02:00 Wrap-up\n")
        job.chapters_path = str(job_dir / "chapters.txt")
    update(job)


def test_decide_only_then_render(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _decided_runner)
    client = TestClient(api_main.app)
    job_id = client.post(
        "/jobs", files={"file": ("m.mp4", io.BytesIO(b"x"), "video/mp4")}, data={"decide_only": "true"}
    ).json()["job_id"]
    assert api_main.job_queue.wait_idle(10)
    assert client.get(f"/jobs/{job_id}").json()["status"] == "decided"
    assert client.get(f"/jobs/{job_id}/selection").json()["shorts"] == []
    assert client.get(f"/jobs/{job_id}/chapters").status_code == 409  # not rendered yet

    rendered = client.post(f"/jobs/{job_id}/render")
    assert rendered.status_code == 200 and rendered.json()["options"]["decide_only"] is False
    assert api_main.job_queue.wait_idle(10)
    assert client.get(f"/jobs/{job_id}").json()["status"] == "done"
    assert client.get(f"/jobs/{job_id}/chapters").text.startswith("00:00 Intro")
    assert client.post(f"/jobs/{job_id}/render").status_code == 409  # only decided jobs
