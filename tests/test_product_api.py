"""The product API (plan.MD P3): /api/v1, ownership, limits, keys, webhooks."""

import io
import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select

import api.main as api_main
import pipeline as pipeline_module
from core.config import get_settings
from core.models import ErrorCode, JobStatus
from db.models import User, WebhookDelivery
from db.session import session_scope
from services import email, jobs_index, webhooks
from tests.product_helpers import (
    CSRF,
    PASSWORD,
    bearer,
    emails_to,
    login,
    make_key,
    make_user,
    new_client,
    signed_in,
    signup,
)

pytestmark = pytest.mark.usefixtures("product")


@pytest.fixture(autouse=True)
def _queue_drained():
    yield
    assert api_main.job_queue.wait_idle(10), "a test left a job running"


def _done(job, update):
    job.status = JobStatus.DONE
    update(job)


def _delivering(job, update):
    from core.models import ArtifactInfo

    job_dir = get_settings().jobs_dir / job.job_id
    (job_dir / "final.mp4").write_bytes(b"final video")
    (job_dir / "bundle.zip").write_bytes(b"PK zip")
    job.artifacts = {"final.mp4": ArtifactInfo(path="final.mp4", mandatory=True)}
    job.output_video_path = str(job_dir / "final.mp4")
    job.bundle_path = str(job_dir / "bundle.zip")
    job.bundle_bytes = 6
    job.status = JobStatus.DONE
    update(job)


def _blocking(started: threading.Event, release: threading.Event):
    def run(job, update):
        job.status = JobStatus.TRANSCRIBING
        update(job)
        started.set()
        release.wait(5)
        job.status = JobStatus.DONE
        update(job)

    return run


def _upload(client, headers=None, path="/api/v1/jobs", **data):
    merged = {**CSRF, **(headers or {})}
    return client.post(path, files={"file": ("meeting.mp4", io.BytesIO(b"fake bytes"), "video/mp4")},
                       data=data, headers=merged)


def _user_id(address: str) -> str:
    with session_scope() as db:
        return db.scalar(select(User.id).where(User.email == address))


# ------------------------------------------------------------------ ownership
def test_jobs_belong_to_whoever_created_them(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _delivering)
    ada = signed_in("ada@example.com")
    bob = signed_in("bob@example.com")
    created = _upload(ada)
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    assert api_main.job_queue.wait_idle(10)
    assert created.json()["owner_id"] == _user_id("ada@example.com")

    for path in (f"/api/v1/jobs/{job_id}", f"/api/v1/jobs/{job_id}/log", f"/api/v1/jobs/{job_id}/bundle",
                 f"/api/v1/jobs/{job_id}/artifacts/final.mp4", f"/api/v1/jobs/{job_id}/webhooks",
                 f"/jobs/{job_id}"):
        assert bob.get(path).status_code == 404, path
        assert ada.get(path).status_code == 200, path
    assert bob.delete(f"/api/v1/jobs/{job_id}", headers=CSRF).status_code == 404
    assert bob.post(f"/api/v1/jobs/{job_id}/render", headers=CSRF).status_code == 404
    assert [j["job_id"] for j in ada.get("/api/v1/jobs").json()["items"]] == [job_id]
    assert bob.get("/api/v1/jobs").json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
    assert [j["job_id"] for j in ada.get("/jobs").json()] == [job_id]  # the unprefixed list too
    assert bob.get("/jobs").json() == []

    make_user("root@example.com", role="admin")
    admin = new_client()
    login(admin, "root@example.com")
    assert admin.get(f"/api/v1/jobs/{job_id}").status_code == 200
    assert admin.get("/api/v1/jobs").json()["total"] == 0  # own jobs by default...
    assert admin.get("/api/v1/jobs?scope=all").json()["total"] == 1  # ...everyone's on request
    assert bob.get("/api/v1/jobs?scope=all").json()["total"] == 0  # not for non-admins


def test_v1_hides_server_paths_and_links_resources(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _delivering)
    ada = signed_in()
    job_id = _upload(ada).json()["job_id"]
    assert api_main.job_queue.wait_idle(10)
    v1 = ada.get(f"/api/v1/jobs/{job_id}").json()
    for field in ("source_path", "bundle_path", "output_video_path"):
        assert field not in v1
    assert "path" not in v1["artifacts"]["final.mp4"]
    assert v1["artifacts"]["final.mp4"]["url"].endswith(f"/api/v1/jobs/{job_id}/artifacts/final.mp4")
    assert v1["links"]["bundle"] == f"http://testserver/api/v1/jobs/{job_id}/bundle"
    assert v1["links"]["app"] == f"http://testserver/app/jobs/{job_id}"
    legacy = ada.get(f"/jobs/{job_id}").json()
    assert legacy["source_path"].endswith("source.mp4") and legacy["links"]["bundle"]


def test_api_key_scopes(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _done)
    user_id = make_user("ada@example.com")
    read_only = make_key(user_id, ["jobs:read"])
    writer = make_key(user_id, ["jobs:read", "jobs:write"])
    client = new_client()
    assert client.get("/api/v1/jobs", headers=bearer(read_only)).status_code == 200
    refused = _upload(client, bearer(read_only))
    assert refused.status_code == 403 and refused.json()["error_code"] == "insufficient_scope"
    created = _upload(client, bearer(writer))
    assert created.status_code == 200 and created.json()["owner_id"] == user_id
    assert api_main.job_queue.wait_idle(10)
    # Zoom was removed: its routes are gone, with or without the /api/v1 prefix
    for path in ("/api/v1/zoom/status", "/api/v1/zoom/recordings", "/zoom/status"):
        assert client.get(path, headers=bearer(writer)).status_code == 404, path
    assert client.post("/api/v1/jobs/zoom", headers=bearer(writer), json={"meeting_uuid": "x"}).status_code in (404, 405)


def test_unverified_accounts_can_look_but_not_create():
    client = signed_in(verified=False)
    refused = _upload(client)
    assert refused.status_code == 403 and refused.json()["error_code"] == "email_not_verified"
    assert client.get("/api/v1/jobs").status_code == 200


# ------------------------------------------------------------------ limits
def test_per_user_queue_limit(monkeypatch):
    monkeypatch.setenv("MAX_QUEUE_DEPTH", "5")
    monkeypatch.setenv("MAX_QUEUED_PER_USER", "1")
    get_settings.cache_clear()
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(api_main, "run_pipeline", _blocking(started, release))
    ada, bob = signed_in("ada@example.com"), signed_in("bob@example.com")
    try:
        assert _upload(ada).status_code == 200
        assert started.wait(5)
        limited = _upload(ada)
        assert limited.status_code == 429 and limited.json()["error_code"] == "too_many_jobs"
        assert limited.headers["Retry-After"] == "60"
        queued = _upload(bob)  # someone else still gets in line
        assert queued.status_code == 200
        assert bob.get(f"/api/v1/jobs/{queued.json()['job_id']}").json()["queue_position"] == 1
        listed = bob.get("/api/v1/jobs?status=active").json()["items"]
        assert listed[0]["queue_position"] == 1
    finally:
        release.set()


def test_monthly_job_limit_spares_admins(monkeypatch):
    monkeypatch.setenv("PLAN_JOBS_PER_MONTH", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(api_main, "run_pipeline", _done)
    ada = signed_in()
    assert _upload(ada).status_code == 200
    assert api_main.job_queue.wait_idle(10)
    over = _upload(ada)
    assert over.status_code == 402 and over.json()["error_code"] == "plan_limit"
    make_user("root@example.com", role="admin")
    admin = new_client()
    login(admin, "root@example.com")
    for _ in range(2):
        assert _upload(admin).status_code == 200
        assert api_main.job_queue.wait_idle(10)


def test_recording_length_limit_stops_the_job_before_any_ai_call(monkeypatch):
    monkeypatch.setenv("PLAN_MAX_MINUTES", "10")
    get_settings.cache_clear()
    reached_ai = []

    def runner(job, update):
        try:
            job.source_duration_s = 3600.0  # what the pipeline records after probing
            job.status = JobStatus.TRANSCRIBING
            update(job)
            reached_ai.append(job.job_id)
            job.status = JobStatus.DONE
            update(job)
        except Exception as exc:  # noqa: BLE001 — mirror run_pipeline's handler
            pipeline_module._fail(job, update, exc)

    monkeypatch.setattr(api_main, "run_pipeline", runner)
    ada = signed_in()
    job_id = _upload(ada).json()["job_id"]
    assert api_main.job_queue.wait_idle(10)
    job = ada.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "failed" and job["error_code"] == "plan_limit" and "60 minutes" in job["error"]
    assert reached_ai == []
    make_user("root@example.com", role="admin")
    admin = new_client()
    login(admin, "root@example.com")
    admin_job = _upload(admin).json()["job_id"]
    assert api_main.job_queue.wait_idle(10)
    assert admin.get(f"/api/v1/jobs/{admin_job}").json()["status"] == "done"


# ------------------------------------------------------------------ keys API
def test_key_management():
    unverified = signed_in("new@example.com", verified=False)
    assert unverified.post("/api/v1/keys", json={"name": "n8n"}, headers=CSRF).json()["error_code"] == \
        "email_not_verified"

    ada = signed_in()
    created = ada.post("/api/v1/keys", json={"name": "  n8n   prod ", "scopes": ["jobs:read"]}, headers=CSRF)
    assert created.status_code == 201
    body = created.json()
    key = body["key"]
    assert re.fullmatch(r"hc_live_[0-9A-Za-z]{8}_[A-Za-z0-9_-]{32}", key)
    assert body["name"] == "n8n prod" and body["scopes"] == ["jobs:read"] and body["display"].endswith("…")
    listed = ada.get("/api/v1/keys").json()
    assert "key" not in listed["items"][0] and key not in json.dumps(listed)
    assert set(listed["scopes"]) == {"jobs:read", "jobs:write"}
    assert ada.post("/api/v1/keys", json={"name": "old", "scopes": ["zoom:read"]}, headers=CSRF).status_code == 422

    client = new_client()
    assert client.get("/api/v1/jobs", headers=bearer(key)).status_code == 200
    assert client.get("/api/v1/keys", headers=bearer(key)).json()["error_code"] == "session_required"

    rotated = ada.post(f"/api/v1/keys/{body['id']}/rotate", headers=CSRF).json()
    assert rotated["key"] != key and rotated["scopes"] == ["jobs:read"]
    assert client.get("/api/v1/jobs", headers=bearer(key)).status_code == 401
    assert client.get("/api/v1/jobs", headers=bearer(rotated["key"])).status_code == 200
    assert ada.delete(f"/api/v1/keys/{rotated['id']}", headers=CSRF).json()["revoked"] is True
    assert client.get("/api/v1/jobs", headers=bearer(rotated["key"])).status_code == 401
    assert ada.delete(f"/api/v1/keys/{rotated['id']}", headers=CSRF).status_code == 404
    bad = ada.post("/api/v1/keys", json={"name": "x", "scopes": ["admin"]}, headers=CSRF)
    assert bad.status_code == 422 and bad.json()["error_code"] == "validation_error"


def test_key_count_is_limited(monkeypatch):
    monkeypatch.setenv("MAX_API_KEYS_PER_USER", "2")
    get_settings.cache_clear()
    ada = signed_in()
    for i in range(2):
        assert ada.post("/api/v1/keys", json={"name": f"k{i}"}, headers=CSRF).status_code == 201
    refused = ada.post("/api/v1/keys", json={"name": "k3"}, headers=CSRF)
    assert refused.status_code == 409 and refused.json()["error_code"] == "too_many_keys"


# ------------------------------------------------------------------ webhooks
class Receiver:
    def __init__(self, status: int = 200):
        self.status = status
        self.requests: list[tuple[dict, bytes]] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                receiver.requests.append(({k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(receiver.status)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/hook?token=abc"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ALLOW_HTTP", "true")
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE", "true")
    get_settings.cache_clear()
    server = Receiver()
    yield server
    server.close()


def test_finished_jobs_send_a_signed_webhook(monkeypatch, receiver):
    monkeypatch.setattr(api_main, "run_pipeline", _delivering)
    ada = signed_in()
    secret = ada.get("/api/v1/auth/webhook-secret").json()["webhook_secret"]
    created = _upload(ada, callback_url=receiver.url)
    assert created.json()["callback_url"] == receiver.url
    job_id = created.json()["job_id"]
    assert api_main.job_queue.wait_idle(10)
    webhooks.process_due()
    assert len(receiver.requests) == 1
    headers, body = receiver.requests[0]
    assert headers["x-hc-event"] == "job.done" and headers["content-type"] == "application/json"
    assert webhooks.verify_signature(secret, body, headers["x-hc-signature"])
    assert not webhooks.verify_signature("whsec_wrong", body, headers["x-hc-signature"])
    assert not webhooks.verify_signature(secret, body + b" ", headers["x-hc-signature"])
    payload = json.loads(body)
    assert payload["event"] == "job.done" and payload["job"]["job_id"] == job_id
    assert "source_path" not in payload["job"] and payload["job"]["links"]["bundle"].endswith("/bundle")
    deliveries = ada.get(f"/api/v1/jobs/{job_id}/webhooks").json()["items"]
    assert deliveries[0]["state"] == "delivered" and deliveries[0]["status_code"] == 200
    webhooks.process_due()
    assert len(receiver.requests) == 1  # delivered once only


def test_failing_receivers_are_retried_then_given_up(monkeypatch, receiver):
    receiver.status = 500
    monkeypatch.setattr(api_main, "run_pipeline", _done)
    ada = signed_in()
    job_id = _upload(ada, callback_url=receiver.url).json()["job_id"]
    assert api_main.job_queue.wait_idle(10)
    now = datetime.now(UTC)
    for minutes in (0, 2, 8, 40, 200):
        webhooks.process_due(now + timedelta(minutes=minutes))
    assert len(receiver.requests) == 4  # the first try and three retries
    delivery = ada.get(f"/api/v1/jobs/{job_id}/webhooks").json()["items"][0]
    assert delivery["state"] == "failed" and delivery["attempts"] == 4 and delivery["error"] == "HTTP 500"


def test_a_receiver_that_recovers_gets_the_retry(monkeypatch, receiver):
    receiver.status = 503
    monkeypatch.setattr(api_main, "run_pipeline", _done)
    ada = signed_in()
    job_id = _upload(ada, callback_url=receiver.url).json()["job_id"]
    assert api_main.job_queue.wait_idle(10)
    webhooks.process_due()
    with session_scope() as db:
        pending = db.scalars(select(WebhookDelivery)).one()
        assert pending.attempt == 1 and pending.next_attempt_at > datetime.now(UTC) + timedelta(seconds=50)
    receiver.status = 204
    webhooks.process_due(datetime.now(UTC) + timedelta(minutes=2))
    assert ada.get(f"/api/v1/jobs/{job_id}/webhooks").json()["items"][0]["state"] == "delivered"


@pytest.mark.parametrize("url", [
    "http://93.184.216.34/hook",           # plain http
    "ftp://93.184.216.34/hook",
    "https://user:pass@93.184.216.34/hook",
    "https://10.0.0.5/hook",               # private
    "https://127.0.0.1/hook",              # this machine
    "https://169.254.169.254/latest",      # cloud metadata
    "https://[::1]/hook",
    "https://100.64.0.1/hook",             # carrier-grade NAT
])
def test_unsafe_callback_urls_are_refused(url):
    ada = signed_in()
    response = _upload(ada, callback_url=url)
    assert response.status_code == 422 and response.json()["error_code"] == "webhook_url_invalid", url


def test_public_callback_urls_are_accepted():
    assert webhooks.validate_callback_url("https://93.184.216.34/hook?x=1#frag") == "https://93.184.216.34/hook?x=1"


def test_webhook_test_button(receiver):
    ada = signed_in()
    result = ada.post("/api/v1/webhooks/test", json={"url": receiver.url}, headers=CSRF).json()
    assert result == {"delivered": True, "status_code": 200, "error": None}
    headers, body = receiver.requests[0]
    assert headers["x-hc-event"] == "ping" and json.loads(body)["event"] == "ping"
    secret = ada.get("/api/v1/auth/webhook-secret").json()["webhook_secret"]
    assert webhooks.verify_signature(secret, body, headers["x-hc-signature"])


# ------------------------------------------------------------------ job emails
def test_owners_are_emailed_when_jobs_finish(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _delivering)
    ada = signed_in()
    _upload(ada, title="Robotics sync")
    assert api_main.job_queue.wait_idle(10)
    message = emails_to("ada@example.com", "job_finished")[-1]
    assert message.subject == "Your meeting “Robotics sync” is ready" and "final.mp4" in message.text

    ada.patch("/api/v1/auth/me", json={"notify_on_done": False}, headers=CSRF)
    before = len(email.outbox())
    _upload(ada)
    assert api_main.job_queue.wait_idle(10)
    assert len(email.outbox()) == before


def test_failed_jobs_email_the_reason(monkeypatch):
    def failing(job, update):
        pipeline_module._fail(job, update, RuntimeError("ffmpeg exploded"))

    monkeypatch.setattr(api_main, "run_pipeline", failing)
    ada = signed_in()
    _upload(ada)
    assert api_main.job_queue.wait_idle(10)
    message = emails_to("ada@example.com", "job_finished")[-1]
    assert "failed" in message.subject and "ffmpeg exploded" in message.text


# ------------------------------------------------------------------ docs, usage, account, admin
def test_published_api_schema():
    client = new_client()
    schema = client.get("/api/v1/openapi.json").json()
    paths = schema["paths"]
    assert "/api/v1/jobs" in paths and "/api/v1/jobs/youtube" in paths and "/api/v1/keys" in paths
    assert not any("zoom" in path for path in paths)
    assert not any(path.startswith(("/jobs", "/zoom")) for path in paths)
    assert set(schema["components"]["securitySchemes"]) == {"BearerAuth", "ApiKeyHeader"}
    assert paths["/api/v1/auth/login"]["post"]["security"] == []
    assert "security" not in paths["/api/v1/jobs"]["get"]  # the global requirement applies
    docs = client.get("/api/docs")
    assert docs.status_code == 200 and "swagger" in docs.text.lower()
    assert client.get("/api/v1/public/config").json()["signup_enabled"] is True


def test_usage(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _delivering)
    ada = signed_in()
    _upload(ada)
    assert api_main.job_queue.wait_idle(10)
    usage = ada.get("/api/v1/account/usage").json()
    assert usage["jobs_total"] == 1 and usage["jobs_this_month"] == 1 and usage["jobs_active"] == 0
    assert usage["bundle_bytes"] == 6 and usage["limits"]["max_queued_per_user"] == 2


def test_account_deletion(monkeypatch):
    monkeypatch.setattr(api_main, "run_pipeline", _delivering)
    ada = signed_in()
    job_id = _upload(ada).json()["job_id"]
    assert api_main.job_queue.wait_idle(10)
    key = ada.post("/api/v1/keys", json={"name": "n8n"}, headers=CSRF).json()["key"]
    wrong = ada.request("DELETE", "/api/v1/account", json={"password": "nope nope nope", "confirm_email": "ada@example.com"},
                        headers=CSRF)
    assert wrong.status_code == 403
    typo = ada.request("DELETE", "/api/v1/account", json={"password": PASSWORD, "confirm_email": "ad@example.com"},
                       headers=CSRF)
    assert typo.status_code == 422
    gone = ada.request("DELETE", "/api/v1/account", json={"password": PASSWORD, "confirm_email": "ADA@example.com"},
                       headers=CSRF)
    assert gone.json() == {"deleted": True, "jobs_deleted": 1}
    assert not (get_settings().jobs_dir / job_id).exists()
    assert login(new_client()).status_code == 401
    assert new_client().get("/api/v1/jobs", headers=bearer(key)).status_code == 401


def test_admin_endpoints(monkeypatch):
    ada = signed_in()
    assert ada.get("/api/v1/admin/users").json()["error_code"] == "forbidden"
    make_user("root@example.com", role="admin")
    admin = new_client()
    login(admin, "root@example.com")
    users = admin.get("/api/v1/admin/users?q=ADA").json()
    assert users["total"] == 1 and users["items"][0]["email"] == "ada@example.com"
    ada_id = users["items"][0]["id"]
    blocked = admin.patch(f"/api/v1/admin/users/{ada_id}", json={"is_active": False}, headers=CSRF)
    assert blocked.json()["is_active"] is False
    assert ada.get("/api/v1/auth/me").status_code == 401
    root_id = _user_id("root@example.com")
    last = admin.patch(f"/api/v1/admin/users/{root_id}", json={"role": "user"}, headers=CSRF)
    assert last.status_code == 409 and last.json()["error_code"] == "last_admin"
    stats = admin.get("/api/v1/admin/stats").json()
    assert stats["users"] == 2 and stats["admins"] == 1 and stats["queue"]["depth"] == 0


def test_job_folders_from_before_accounts_go_to_the_first_admin():
    job_dir = get_settings().jobs_dir / "old-job"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(json.dumps({"job_id": "old-job", "status": "done", "source_path": "x.mp4",
                                                  "title": "Before accounts"}), encoding="utf-8")
    make_user("root@example.com", role="admin")
    assert jobs_index.index_orphans() == 1
    assert jobs_index.index_orphans() == 0
    admin = new_client()
    login(admin, "root@example.com")
    items = admin.get("/api/v1/jobs").json()["items"]
    assert [(i["job_id"], i["title"]) for i in items] == [("old-job", "Before accounts")]


def test_job_folders_owned_by_a_user_the_database_no_longer_has_still_index():
    # a fresh database next to old job folders: their owner ids point at nobody
    job_dir = get_settings().jobs_dir / "orphan-job"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(json.dumps({"job_id": "orphan-job", "status": "done", "source_path": "x.mp4",
                                                  "owner_id": "user-from-an-old-database"}), encoding="utf-8")
    assert jobs_index.index_orphans() == 1  # no foreign-key failure at startup
    make_user("root@example.com", role="admin")
    jobs_index.index_orphans()  # unowned rows go to the first admin
    admin = new_client()
    login(admin, "root@example.com")
    assert [i["job_id"] for i in admin.get("/api/v1/jobs").json()["items"]] == ["orphan-job"]


def test_operator_jobs_go_to_the_first_admin(monkeypatch):
    monkeypatch.setenv("HC_API_TOKEN", "ops-secret-value")
    get_settings.cache_clear()
    monkeypatch.setattr(api_main, "run_pipeline", _done)
    make_user("root@example.com", role="admin")
    created = _upload(new_client(), {"X-API-Key": "ops-secret-value"}, path="/jobs")
    assert created.json()["owner_id"] == _user_id("root@example.com")
    assert api_main.job_queue.wait_idle(10)


def test_the_unprefixed_api_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("LEGACY_API_ENABLED", "false")
    get_settings.cache_clear()
    ada = signed_in()
    moved = ada.get("/jobs")
    assert moved.status_code == 404 and "/api/v1/jobs" in moved.json()["detail"]
    assert ada.get("/api/v1/jobs").status_code == 200


def test_every_error_code_the_code_raises_is_a_known_job_error_code():
    """A job.json with an unknown error_code can't be loaded after a restart
    (JobRecord.error_code is a Literal): keep the list complete."""
    known = set(ErrorCode.__args__)
    root = Path(__file__).resolve().parent.parent
    raised = set()
    for folder in ("ingest", "decide", "edl", "slice", "core", "bundle", "report", "transcribe"):
        for path in (root / folder).rglob("*.py"):
            raised |= set(re.findall(r'code\s*=\s*"([a-z_]+)"', path.read_text(encoding="utf-8")))
    for path in ("pipeline.py", "deliver.py", "outputs.py"):
        raised |= set(re.findall(r'code\s*=\s*"([a-z_]+)"', (root / path).read_text(encoding="utf-8")))
    raised |= {"plan_limit"}
    assert raised - known == set()


def test_index_follows_status_and_title(monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(api_main, "run_pipeline", _blocking(started, release))
    ada = signed_in()
    job_id = _upload(ada, title="Weekly").json()["job_id"]
    try:
        assert started.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = ada.get("/api/v1/jobs").json()["items"][0]
            if item["status"] == "transcribing":
                break
            time.sleep(0.05)
        assert item["status"] == "transcribing" and item["title"] == "Weekly" and item["queue_position"] == 0
    finally:
        release.set()
    assert api_main.job_queue.wait_idle(10)
    done = ada.get("/api/v1/jobs?status=done").json()
    assert done["total"] == 1 and done["items"][0]["job_id"] == job_id and done["items"][0]["finished_at"]
    assert ada.get("/api/v1/jobs?status=active").json()["total"] == 0


def test_signup_then_first_job_by_api_key_end_to_end(monkeypatch):
    """The whole automation story: sign up, verify, make a key, create a job
    with the key, poll it, download the bundle."""
    monkeypatch.setattr(api_main, "run_pipeline", _delivering)
    browser = new_client()
    assert signup(browser, "maker@example.com").status_code == 201
    from tests.product_helpers import verify

    assert verify(browser, "maker@example.com").status_code == 200
    key = browser.post("/api/v1/keys", json={"name": "script"}, headers=CSRF).json()["key"]
    script = new_client()
    job = _upload(script, bearer(key)).json()
    assert api_main.job_queue.wait_idle(10)
    status = script.get(f"/api/v1/jobs/{job['job_id']}", headers=bearer(key)).json()
    assert status["status"] == "done"
    bundle = script.get(status["links"]["bundle"].replace("http://testserver", ""), headers=bearer(key))
    assert bundle.status_code == 200 and bundle.content == b"PK zip"


def test_youtube_jobs_accept_only_youtube_links(monkeypatch):
    """A job's link is fetched by this server: anything but YouTube is refused
    before a job exists (no internal addresses, no other sites)."""
    ada = signed_in()
    for url in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8000/health",
                "https://example.com/talk.mp4"):
        refused = ada.post("/api/v1/jobs/youtube", json={"url": url}, headers=CSRF)
        assert refused.status_code == 422, url
        assert refused.json()["error_code"] == "validation_error"
    assert ada.get("/api/v1/jobs").json()["total"] == 0
