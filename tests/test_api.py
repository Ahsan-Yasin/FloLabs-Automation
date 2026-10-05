import io
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

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
        data={"decide_only": "true", "shorts_count": "2", "highlights_criteria": "  jokes about coffee ",
              "cut_silence": "false", "intro_outro": "false"},
    )
    assert response.status_code == 200
    options = response.json()["options"]
    assert options["decide_only"] is True and options["shorts_count"] == 2
    assert options["highlights_criteria"] == "jokes about coffee"
    assert options["cut_silence"] is False  # the UI's "Cut silences" box, unticked
    assert options["intro_outro"] is False  # the UI's "Add intro & outro" box, unticked
    bad = client.post("/jobs", files={"file": ("m.mp4", io.BytesIO(b"x"), "video/mp4")}, data={"shorts_count": "99"})
    assert bad.status_code == 422


def _delivering_runner(job, update):
    """A done job with a bundle, two artifacts and one failed optional one."""
    from core.models import ArtifactInfo

    job_dir = get_settings().jobs_dir / job.job_id
    (job_dir / "shorts").mkdir(parents=True, exist_ok=True)
    (job_dir / "final.mp4").write_bytes(b"final video")
    (job_dir / "shorts" / "short_01.mp4").write_bytes(b"short")
    (job_dir / "secret.txt").write_text("not an artifact")
    (job_dir / "bundle.zip").write_bytes(b"PK zip")
    job.artifacts = {
        "final.mp4": ArtifactInfo(path="final.mp4", mandatory=True),
        "shorts/short_01.mp4": ArtifactInfo(path="shorts/short_01.mp4"),
        "removed.mp4": ArtifactInfo(path="removed.mp4", status="failed", reason="boom"),
    }
    job.output_video_path = str(job_dir / "final.mp4")
    job.bundle_path = str(job_dir / "bundle.zip")
    job.status = JobStatus.DONE
    update(job)


def test_bundle_and_artifacts_are_served_by_name_only(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _delivering_runner)
    client = TestClient(api_main.app)
    created = client.post("/jobs", files={"file": ("Weekly_Sync-2026.mp4", io.BytesIO(b"x"), "video/mp4")}).json()
    assert created["title"] == "Weekly Sync 2026"
    job_id = created["job_id"]
    done = _wait_until_done(client, job_id).json()
    assert done["status"] == "done"
    # an old-style job (its video saved as final.mp4) still downloads under
    # the owner's names: Final_<Meeting>_<date>.zip / ..._Youtube.mp4, the
    # date being the day the job was created (no date in the title), on the
    # team's clock (settings.local_timezone), not UTC
    day = datetime.fromisoformat(done["created_at"].replace("Z", "+00:00")).astimezone(
        ZoneInfo("Asia/Karachi")).date().isoformat()

    bundle = client.get(f"/jobs/{job_id}/bundle")
    assert bundle.status_code == 200 and bundle.content == b"PK zip"
    assert bundle.headers["content-disposition"] == f'attachment; filename="Final_WeeklySync2026_{day}.zip"'
    assert client.get(f"/jobs/{job_id}/bundle", headers={"Range": "bytes=3-5"}).content == b"zip"

    short = client.get(f"/jobs/{job_id}/artifacts/shorts/short_01.mp4")
    assert short.status_code == 200 and short.headers["content-type"] == "video/mp4"
    final = client.get(f"/jobs/{job_id}/artifacts/final.mp4?download=true")
    assert final.content == b"final video"
    assert final.headers["content-disposition"] == f'attachment; filename="Final_WeeklySync2026_{day}_Youtube.mp4"'
    video = client.get(f"/jobs/{job_id}/video")
    # plays in the browser, saves under the owner's name
    assert video.headers["content-disposition"] == f'inline; filename="Final_WeeklySync2026_{day}_Youtube.mp4"'
    assert client.get(f"/jobs/{job_id}/artifacts/removed.mp4").status_code == 404  # failed artifact
    assert client.get(f"/jobs/{job_id}/artifacts/secret.txt").status_code == 404  # not an artifact
    assert client.get(f"/jobs/{job_id}/artifacts/..%2Fjob.json").status_code == 404

    (get_settings().jobs_dir / job_id / "final.mp4").unlink()
    assert client.get(f"/jobs/{job_id}/video").status_code == 410
    assert client.get(f"/jobs/{job_id}/artifacts/final.mp4").status_code == 410


def test_upload_title_field_overrides_the_file_name(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _fake_run_pipeline)
    client = TestClient(api_main.app)
    job = client.post("/jobs", files={"file": ("source.mp4", io.BytesIO(b"x"), "video/mp4")},
                      data={"title": "  Robotics   weekly  "}).json()
    assert job["title"] == "Robotics weekly"
    _wait_until_done(client, job["job_id"])


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


# ---------------------------------------------------------------- service hardening


def _failed_with_picks(job_id, **fields):
    """A job whose render failed after the picks were saved (what a crash,
    a restart or a retryable ffmpeg error leaves behind)."""
    job_dir = get_settings().jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "selection.json").write_text(json.dumps({"moments": [], "highlights": {}, "shorts": []}))
    (job_dir / "decisions.json").write_text("[]")
    defaults = {
        "status": "failed",
        "error_code": "interrupted",
        "error": "the service stopped while this job was slicing; submit it again",
        "retryable": True,
        "selection_path": str(job_dir / "selection.json"),
        "decisions_path": str(job_dir / "decisions.json"),
    }
    return _write_job(job_id, **{**defaults, **fields})


def test_failed_render_with_saved_picks_can_be_rendered_again(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _fake_run_pipeline)
    client = TestClient(api_main.app)
    _failed_with_picks("failed-render")
    # the UI shows the picks again, so the selection must stay readable
    assert client.get("/jobs/failed-render/selection").status_code == 200

    rendered = client.post("/jobs/failed-render/render")
    assert rendered.status_code == 200
    body = rendered.json()
    assert body["status"] == "queued" and body["options"]["decide_only"] is False
    assert body["error_code"] is None and body["retryable"] is False
    assert api_main.job_queue.wait_idle(10)
    assert client.get("/jobs/failed-render").json()["status"] == "done"


def test_interrupted_render_can_be_rendered_again(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _fake_run_pipeline)
    _failed_with_picks("cut-short", status="slicing", error_code=None, error=None, retryable=False)
    reconcile_on_startup(job_store)  # the restart marks it failed/interrupted
    client = TestClient(api_main.app)
    assert client.post("/jobs/cut-short/render").status_code == 200
    assert api_main.job_queue.wait_idle(10)
    assert client.get("/jobs/cut-short").json()["status"] == "done"


def test_failed_job_without_saved_picks_or_retry_cannot_be_rendered():
    client = TestClient(api_main.app)
    _failed_with_picks("not-retryable", retryable=False, error_code="render_assert_failed")
    _failed_with_picks("no-picks", selection_path=None)
    assert client.post("/jobs/not-retryable/render").status_code == 409
    assert client.post("/jobs/no-picks/render").status_code == 409
    assert client.get("/jobs/not-retryable/selection").status_code == 409


def test_unauthenticated_upload_is_rejected_before_the_body_is_read(monkeypatch):
    """The key check must run before FastAPI parses the multipart body, or an
    anonymous multi-GB upload is spooled to disk just to be answered 401."""
    import asyncio

    monkeypatch.setenv("HC_API_TOKEN", "s3cret")
    get_settings.cache_clear()
    body_reads = []

    async def receive():
        body_reads.append(1)
        raise AssertionError("the upload body was read before the API key was checked")

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/jobs", "raw_path": b"/jobs", "root_path": "", "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"multipart/form-data; boundary=xyz"),
            (b"content-length", str(5 * 1024**3).encode()),
        ],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
    }
    asyncio.run(api_main.app(scope, receive, send))
    assert body_reads == []
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 401
    assert json.loads(sent[1]["body"])["detail"]["error_code"] == "unauthorized"
    assert not any(get_settings().jobs_dir.iterdir())  # nothing was stored


def test_api_docs_are_not_served(monkeypatch):
    client = TestClient(api_main.app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    monkeypatch.setenv("HC_API_TOKEN", "s3cret")
    get_settings.cache_clear()
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/openapi.json", headers={"X-API-Key": "s3cret"}).status_code == 404


def test_url_encoded_api_key_cookie_is_accepted(monkeypatch):
    monkeypatch.setenv("HC_API_TOKEN", "p@ss word")
    get_settings.cache_clear()
    client = TestClient(api_main.app)
    client.cookies.set("hc_api_key", "p%40ss%20word")  # what the web UI's encodeURIComponent writes
    assert client.get("/jobs/nope").status_code == 404


def test_odd_job_ids_never_reach_the_filesystem(monkeypatch):
    """On Windows "<id>." opens the same folder as "<id>", so DELETE used to
    miss the running-job guard and wipe a running job's folder; a decoded
    %5C is a path separator there too."""
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(api_main, "run_pipeline", _blocking_runner(started, release))
    client = TestClient(api_main.app)
    job_id = _upload(client).json()["job_id"]
    assert started.wait(5)
    job_dir = get_settings().jobs_dir / job_id
    try:
        for odd in (f"{job_id}.", f"{job_id}. ", "..%5C..", f"..%5Cjobs%5C{job_id}", "a" * 65):
            assert client.get(f"/jobs/{odd}").status_code == 404, odd
            assert client.delete(f"/jobs/{odd}?force=true").status_code == 404, odd
            assert client.post(f"/jobs/{odd}/render").status_code == 404, odd
            assert client.get(f"/jobs/{odd}/log").status_code == 404, odd
            assert client.get(f"/jobs/{odd}/selection").status_code == 404, odd
        assert job_dir.exists()
        assert client.get(f"/jobs/{job_id}").json()["status"] == "transcribing"
    finally:
        release.set()


def test_upload_that_fails_midway_leaves_no_job_folder(monkeypatch):
    def disk_full(filename, fileobj, job_id=None):
        dest = get_settings().jobs_dir / job_id / "source.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"half a file")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(api_main, "store_video", disk_full)
    client = TestClient(api_main.app, raise_server_exceptions=False)
    assert _upload(client).status_code == 500
    assert not any(get_settings().jobs_dir.iterdir())


def test_transcript_that_fails_midway_leaves_no_job_folder(monkeypatch):
    def disk_full(filename, fileobj, job_id=None):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(api_main, "store_transcript", disk_full)
    client = TestClient(api_main.app, raise_server_exceptions=False)
    response = client.post(
        "/jobs",
        files={
            "file": ("meeting.mp4", io.BytesIO(b"fake bytes"), "video/mp4"),
            "transcript": ("meeting.vtt", io.BytesIO(b"WEBVTT"), "text/vtt"),
        },
    )
    assert response.status_code == 500
    assert not any(get_settings().jobs_dir.iterdir())


def test_rejected_transcript_type_leaves_no_job_folder():
    client = TestClient(api_main.app)
    bad = client.post(
        "/jobs",
        files={
            "file": ("meeting.mp4", io.BytesIO(b"fake bytes"), "video/mp4"),
            "transcript": ("meeting.txt", io.BytesIO(b"hi"), "text/plain"),
        },
    )
    assert bad.status_code == 400
    assert not any(get_settings().jobs_dir.iterdir())


def test_upload_copy_does_not_block_the_event_loop(monkeypatch):
    """Copying a multi-GB upload must run off the event loop, or /health and
    job polling stall for the whole copy."""
    import ingest.store as store_module

    entered, release = threading.Event(), threading.Event()

    def slow_store_video(filename, fileobj, job_id=None):
        entered.set()
        release.wait(5)
        return store_module.store_video(filename, fileobj, job_id=job_id)

    monkeypatch.setattr(api_main, "store_video", slow_store_video)
    monkeypatch.setattr(api_main, "run_pipeline", _fake_run_pipeline)
    with TestClient(api_main.app) as client:  # one shared event loop for both requests
        uploader = threading.Thread(target=lambda: _upload(client))
        uploader.start()
        try:
            assert entered.wait(5)
            t0 = time.monotonic()
            assert client.get("/health").status_code == 200
            assert time.monotonic() - t0 < 2
        finally:
            release.set()
            uploader.join(10)


def _youtube_runner(source_path):
    def run(job, update, url):
        job.source_path = str(source_path)
        job.status = JobStatus.DONE
        update(job)

    return run


def test_delete_removes_a_youtube_download_from_videos(monkeypatch):
    download = get_settings().videos_dir / "0123abcd.mp4"
    download.write_bytes(b"downloaded")
    monkeypatch.setattr(api_main, "run_youtube_pipeline", _youtube_runner(download))
    client = TestClient(api_main.app)
    job_id = client.post("/jobs/youtube", json={"url": "https://youtube.com/watch?v=abc"}).json()["job_id"]
    assert api_main.job_queue.wait_idle(10)
    assert client.delete(f"/jobs/{job_id}").status_code == 200
    assert not download.exists()


def test_delete_never_touches_a_source_outside_videos_or_one_shared_by_another_job(monkeypatch, tmp_path):
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"not ours")
    nested = get_settings().videos_dir / "nested"
    nested.mkdir()
    (nested / "clip.mp4").write_bytes(b"not a download")
    shared = get_settings().videos_dir / "shared.mp4"
    shared.write_bytes(b"two jobs")
    _write_job("outside-src", status="done", source_path=str(outside))
    _write_job("nested-src", status="done", source_path=str(nested / "clip.mp4"))
    _write_job("shared-a", status="done", source_path=str(shared))
    _write_job("shared-b", status="done", source_path=str(shared))
    client = TestClient(api_main.app)
    for job_id in ("outside-src", "nested-src", "shared-a"):
        assert client.delete(f"/jobs/{job_id}").status_code == 200
    assert outside.exists() and (nested / "clip.mp4").exists() and shared.exists()
    assert client.delete("/jobs/shared-b").status_code == 200  # now the last owner
    assert not shared.exists()


def test_protected_job_keeps_its_folder_and_its_videos_source(monkeypatch):
    """Mirrors the regression fixture (job 7b93… with a .keep marker and its
    source in videos/): neither DELETE nor a direct cleanup may remove it."""
    from api.queue import delete_job_files

    source = get_settings().videos_dir / "fixture.mp4"
    source.write_bytes(b"98 minutes")
    job_dir = _write_job("7b93aa4cd7ef436895213e6fff9365a3", status="done", source_path=str(source))
    (job_dir / ".keep").write_text("")
    client = TestClient(api_main.app)
    refused = client.delete("/jobs/7b93aa4cd7ef436895213e6fff9365a3?force=true")
    assert refused.status_code == 409 and refused.json()["detail"]["error_code"] == "protected"
    delete_job_files(job_store, "7b93aa4cd7ef436895213e6fff9365a3")
    assert (job_dir / "job.json").exists() and source.exists()
    assert client.get("/jobs/7b93aa4cd7ef436895213e6fff9365a3").status_code == 200


def test_retention_sweep_removes_a_youtube_download(monkeypatch):
    monkeypatch.setenv("JOB_RETENTION_HOURS", "1")
    get_settings.cache_clear()
    download = get_settings().videos_dir / "feedbeef.mp4"
    download.write_bytes(b"downloaded")
    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    _write_job("old-youtube", status="done", source_path=str(download), updated_at=old)
    assert reconcile_on_startup(job_store)["deleted"] == 1
    assert not download.exists()
