"""The Zoom routes with a faked Zoom client: no real Zoom calls."""

import io
import json
import threading
from datetime import UTC, date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core.config import get_settings
from core.errors import PipelineError
from core.models import JobStatus
from ingest import zoom


@pytest.fixture(autouse=True)
def _queue_drained():
    yield
    assert api_main.job_queue.wait_idle(10), "a test left a job running"


@pytest.fixture
def zoom_env(monkeypatch):
    monkeypatch.setenv("ZOOM_ACCOUNT_ID", "acct-SECRET-ish")
    monkeypatch.setenv("ZOOM_CLIENT_ID", "client-SECRET-ish")
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "very-secret-value")
    monkeypatch.setenv("ZOOM_HOST_EMAIL", "host@example.com")
    get_settings.cache_clear()


def _meeting(*, video_status="completed", transcript=True, end=None):
    end = end or datetime.now(UTC).isoformat()
    span = {"recording_start": "2026-09-20T10:00:00Z", "recording_end": end}
    files = [{"file_type": "MP4", "recording_type": "active_speaker", "download_url": "https://zoom.us/v",
              "status": video_status, **span}]
    if transcript:
        files.append({"file_type": "TRANSCRIPT", "recording_type": "audio_transcript",
                      "download_url": "https://zoom.us/t", "status": "completed", **span})
    return {"uuid": "abc/def==", "id": 42, "topic": "Robotics sync", "start_time": "2026-09-20T10:00:00Z",
            "duration": 60, "host_email": "host@example.com", "recording_files": files}


class FakeClient:
    def __init__(self, meeting=None, error=None):
        self.meeting = meeting or _meeting()
        self.error = error
        self.listed = []

    def get_meeting(self, uuid):
        if self.error:
            raise self.error
        return self.meeting

    def list_recordings(self, host_email, from_date, to_date):
        if self.error:
            raise self.error
        self.listed.append((host_email, from_date, to_date))
        return [zoom.summarize(self.meeting)]


def _use(monkeypatch, client):
    monkeypatch.setattr(zoom, "get_client", lambda: client)
    return client


def _done_runner(calls):
    def run(job, update):
        calls.append(job.job_id)
        job.status = JobStatus.DONE
        update(job)

    return run


def _post(client, uuid="abc/def==", **options):
    return client.post("/jobs/zoom", json={"meeting_uuid": uuid, "options": options})


def test_status_reports_configuration_without_leaking_secrets(zoom_env):
    response = TestClient(api_main.app).get("/zoom/status")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True and body["host_email"] == "host@example.com"
    assert "SECRET" not in response.text and "very-secret-value" not in response.text


def test_not_configured_is_a_400_zoom_auth(monkeypatch):
    client = TestClient(api_main.app)
    assert client.get("/zoom/status").json()["configured"] is False
    for response in (_post(client), client.get("/zoom/recordings?host=a@b.com")):
        assert response.status_code == 400
        assert response.json()["error_code"] == "zoom_auth"
        assert "ZOOM_CLIENT_SECRET" in response.json()["detail"]


def test_recordings_default_to_the_configured_host_and_the_last_30_days(zoom_env, monkeypatch):
    fake = _use(monkeypatch, FakeClient())
    client = TestClient(api_main.app)
    body = client.get("/zoom/recordings").json()
    host, start, end = fake.listed[0]
    assert host == "host@example.com" and (end - start).days == 29 and end == datetime.now(UTC).date()
    assert body["meetings"][0]["uuid"] == "abc/def==" and body["meetings"][0]["status"] == "ready"

    client.get("/zoom/recordings?host=other@example.com&from=2026-01-01&to=2026-03-15")
    assert fake.listed[-1] == ("other@example.com", date(2026, 1, 1), date(2026, 3, 15))
    assert client.get("/zoom/recordings?from=2026-13-01").status_code == 422
    assert client.get("/zoom/recordings?from=2026-03-01&to=2026-01-01").status_code == 400


def test_zoom_errors_map_to_http_statuses(zoom_env, monkeypatch):
    client = TestClient(api_main.app)
    _use(monkeypatch, FakeClient(error=PipelineError("Zoom refused", code="zoom_auth")))
    refused = _post(client)
    assert refused.status_code == 502 and refused.json()["error_code"] == "zoom_auth"
    _use(monkeypatch, FakeClient(error=PipelineError("no such recording", code="zoom_not_found")))
    missing = _post(client)
    assert missing.status_code == 404 and missing.json()["error_code"] == "zoom_not_found"


def test_a_zoom_token_outage_is_a_retryable_502_for_n8n(zoom_env, monkeypatch):
    # the real client: Zoom's token endpoint keeps answering 503
    def handler(request):
        return httpx.Response(503, text="Service Unavailable")

    _use(monkeypatch, zoom.ZoomClient(transport=httpx.MockTransport(handler), sleep=lambda s: None))
    client = TestClient(api_main.app)
    for response in (_post(client), client.get("/zoom/recordings")):
        assert response.status_code == 502
        body = response.json()
        assert body["error_code"] == "zoom_unavailable" and body["retryable"] is True
        assert response.headers["Retry-After"] == str(zoom.UNAVAILABLE_RETRY_AFTER_S)
        assert "ACTIVATED" not in body["detail"] and "very-secret-value" not in response.text
    assert api_main.job_store.list_ids() == []


def test_not_ready_recordings_answer_425_with_a_retry_hint(zoom_env, monkeypatch):
    client = TestClient(api_main.app)
    _use(monkeypatch, FakeClient(_meeting(video_status="processing")))
    response = _post(client)
    assert response.status_code == 425
    assert response.json() | {"detail": ""} == {"error_code": "recording_not_ready", "retryable": True,
                                                 "retry_after_s": 300, "detail": ""}
    assert response.headers["Retry-After"] == "300"

    _use(monkeypatch, FakeClient(_meeting(transcript=False)))  # just recorded: Zoom is still transcribing
    response = _post(client)
    assert response.status_code == 425 and response.json()["error_code"] == "transcript_not_ready"
    assert not any(get_settings().jobs_dir.iterdir())  # no job was created


def test_an_old_recording_without_transcript_depends_on_require_native(zoom_env, monkeypatch):
    calls = []
    monkeypatch.setattr(api_main, "run_zoom_pipeline", _done_runner(calls))
    _use(monkeypatch, FakeClient(_meeting(transcript=False, end="2020-01-01T10:00:00Z")))
    client = TestClient(api_main.app)
    assert _post(client).status_code == 200  # transcribed here
    assert api_main.job_queue.wait_idle(10) and len(calls) == 1

    monkeypatch.setenv("REQUIRE_NATIVE_TRANSCRIPT", "true")
    get_settings.cache_clear()
    refused = _post(client, shorts_count=1)
    assert refused.status_code == 422
    assert refused.json()["error_code"] == "transcript_not_ready" and refused.json()["retryable"] is False


def test_same_meeting_and_options_return_the_existing_job(zoom_env, monkeypatch):
    calls = []
    monkeypatch.setattr(api_main, "run_zoom_pipeline", _done_runner(calls))
    _use(monkeypatch, FakeClient())
    client = TestClient(api_main.app)

    first = _post(client, decide_only=True)
    assert first.status_code == 200
    body = first.json()
    assert body["deduplicated"] is False and body["zoom_meeting_uuid"] == "abc/def=="
    assert body["title"] == "Robotics sync" and body["zoom_meeting"]["meeting_id"] == 42
    assert api_main.job_queue.wait_idle(10)

    again = _post(client, decide_only=True).json()
    assert again["deduplicated"] is True and again["job_id"] == body["job_id"] and len(calls) == 1
    other = _post(client, decide_only=False).json()  # different options: a new job
    assert other["deduplicated"] is False and other["job_id"] != body["job_id"]
    assert api_main.job_queue.wait_idle(10)

    failed = api_main.job_store.get(other["job_id"])
    failed.status = JobStatus.FAILED
    api_main.job_store.update(failed)
    retried = _post(client, decide_only=False).json()  # a failed job is not reused
    assert retried["deduplicated"] is False and retried["job_id"] != other["job_id"]


def test_zoom_job_while_busy_gets_503(zoom_env, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def blocking(job, update):
        job.status = JobStatus.TRANSCRIBING
        update(job)
        started.set()
        release.wait(5)
        job.status = JobStatus.DONE
        update(job)

    monkeypatch.setattr(api_main, "run_pipeline", blocking)
    _use(monkeypatch, FakeClient())
    client = TestClient(api_main.app)
    client.post("/jobs", files={"file": ("m.mp4", io.BytesIO(b"x"), "video/mp4")})
    try:
        assert started.wait(5)
        busy = _post(client)
        assert busy.status_code == 503 and busy.json()["error_code"] == "busy"
    finally:
        release.set()


def test_render_route_uses_the_zoom_runner_for_zoom_jobs(zoom_env, monkeypatch):
    calls = []
    monkeypatch.setattr(api_main, "run_zoom_pipeline", _done_runner(calls))
    monkeypatch.setattr(api_main, "run_pipeline", lambda job, update: pytest.fail("used the upload runner"))
    job_dir = get_settings().jobs_dir / "zoom-decided"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(json.dumps({
        "job_id": "zoom-decided", "status": "decided", "source_path": str(job_dir / "source.mp4"),
        "zoom_meeting_uuid": "abc/def==", "options": {"decide_only": True},
    }), encoding="utf-8")
    client = TestClient(api_main.app)
    assert client.post("/jobs/zoom-decided/render").status_code == 200
    assert api_main.job_queue.wait_idle(10)
    assert calls == ["zoom-decided"]
    assert client.get("/jobs/zoom-decided").json()["status"] == "done"


def test_youtube_and_upload_routes_still_work_next_to_zoom(zoom_env, monkeypatch):
    calls = []
    monkeypatch.setattr(api_main, "run_pipeline", lambda job, update: calls.append("upload"))
    monkeypatch.setattr(api_main, "run_youtube_pipeline", lambda job, update, url: calls.append(url))
    client = TestClient(api_main.app)
    assert client.post("/jobs/youtube", json={"url": "https://youtu.be/x"}).status_code == 200
    assert api_main.job_queue.wait_idle(10)
    assert client.post("/jobs", files={"file": ("m.mp4", io.BytesIO(b"x"), "video/mp4")}).status_code == 200
    assert api_main.job_queue.wait_idle(10)
    assert calls == ["https://youtu.be/x", "upload"]
