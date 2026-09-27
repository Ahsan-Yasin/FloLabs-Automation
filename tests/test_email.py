"""Email backends, templates and delivery (plan.MD P2)."""

import logging
import threading
from typing import ClassVar

import pytest

from core.config import get_settings
from services import email, notifications
from tests.product_helpers import new_client, signup


def _message(**overrides) -> email.OutgoingEmail:
    fields = {"to": "ada@example.com", "subject": "Hello", "text": "plain body", "html": "<p>html body</p>",
              "tags": {"kind": "test"}}
    return email.OutgoingEmail(**{**fields, **overrides})


@pytest.mark.parametrize(("template", "context"), [
    ("verify_email", {"name": "Ada", "link": "http://testserver/verify?token=abc", "hours": 24}),
    ("welcome", {"name": "", "app_link": "http://testserver/app", "keys_link": "http://testserver/app/keys",
                 "docs_link": "http://testserver/docs"}),
    ("reset_password", {"name": "Ada", "link": "http://testserver/reset?token=abc", "minutes": 60}),
    ("password_changed", {"name": "Ada", "forgot_link": "http://testserver/forgot"}),
    ("job_finished", {"name": "Ada", "title": "Weekly <sync>", "status": "done", "error_code": "", "error": "",
                      "retryable": False, "outputs": ["final.mp4", "report.pdf"], "link": "http://testserver/app/jobs/1"}),
    ("job_finished", {"name": "Ada", "title": "Weekly", "status": "decided", "error_code": "", "error": "",
                      "retryable": False, "outputs": [], "link": "http://testserver/app/jobs/1"}),
    ("job_finished", {"name": "Ada", "title": "Weekly", "status": "failed", "error_code": "llm_error",
                      "error": "quota <gone>", "retryable": True, "outputs": [], "link": "http://testserver/app/jobs/1"}),
    ("test", {"backend": "memory"}),
])
def test_every_template_renders_both_twins(template, context):
    text, html = email.render(template, **context)
    for body in (text, html):
        assert "{{" not in body and "{%" not in body
        assert get_settings().app_name in body
    for value in context.values():
        if isinstance(value, str) and value.startswith("http"):
            assert value in text and value in html
    assert html.lstrip().startswith("<!doctype html>")
    if "title" in context and "<" in context["title"]:
        assert "Weekly &lt;sync&gt;" in html and "Weekly <sync>" in text  # HTML escaped, text not
    if context.get("error"):
        assert "quota &lt;gone&gt;" in html


def test_memory_and_console_backends(monkeypatch, caplog):
    email.send(_message())
    assert [m.subject for m in email.outbox()] == ["Hello"]
    monkeypatch.setenv("EMAIL_BACKEND", "console")
    get_settings.cache_clear()
    with caplog.at_level(logging.INFO, logger="services.email"):
        email.send(_message(subject="Printed"))
    assert "Printed" in caplog.text and "plain body" in caplog.text


def test_subjects_cannot_inject_headers():
    email.send(_message(subject="Hi\r\nBcc: victim@example.com"))
    assert email.outbox()[-1].subject == "Hi Bcc: victim@example.com"


class _FakeSMTP:
    instances: ClassVar[list] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        self.sent.append(message)


def _smtp_env(monkeypatch, tls="starttls"):
    monkeypatch.setenv("EMAIL_BACKEND", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "465" if tls == "ssl" else "587")
    monkeypatch.setenv("SMTP_USER", "mailer")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-pass")
    monkeypatch.setenv("SMTP_TLS", tls)
    monkeypatch.setenv("EMAIL_FROM", "Highlight Cutter <no-reply@cut.example.com>")
    monkeypatch.setenv("EMAIL_REPLY_TO", "help@cut.example.com")
    get_settings.cache_clear()
    _FakeSMTP.instances.clear()


def test_smtp_backend_starttls(monkeypatch):
    _smtp_env(monkeypatch)
    monkeypatch.setattr(email.smtplib, "SMTP", _FakeSMTP)
    email.SmtpBackend().send(_message())
    server = _FakeSMTP.instances[-1]
    assert (server.host, server.port) == ("smtp.example.com", 587)
    assert server.calls == ["ehlo", "starttls", "ehlo", ("login", "mailer", "smtp-pass")]
    sent = server.sent[0]
    assert sent["To"] == "ada@example.com" and sent["Reply-To"] == "help@cut.example.com"
    assert sent["Message-ID"].endswith("@cut.example.com>")
    parts = {part.get_content_type(): part.get_content() for part in sent.iter_parts()}
    assert parts["text/plain"].strip() == "plain body" and "html body" in parts["text/html"]


def test_smtp_backend_ssl(monkeypatch):
    _smtp_env(monkeypatch, tls="ssl")
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", _FakeSMTP)
    email.SmtpBackend().send(_message())
    server = _FakeSMTP.instances[-1]
    assert server.port == 465 and "starttls" not in server.calls and server.sent


def test_resend_backend(monkeypatch):
    monkeypatch.setenv("EMAIL_BACKEND", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("EMAIL_FROM", "HC <no-reply@cut.example.com>")
    get_settings.cache_clear()
    calls = []

    class Response:
        def __init__(self, status):
            self.status_code, self.text = status, "boom"

    def fake_post(url, json, timeout, headers):
        calls.append((url, json, headers))
        return Response(200 if len(calls) == 1 else 500)

    monkeypatch.setattr(email.httpx, "post", fake_post)
    email.ResendBackend().send(_message())
    url, payload, headers = calls[0]
    assert url == email.RESEND_URL and headers["Authorization"] == "Bearer re_test_key"
    assert payload["to"] == ["ada@example.com"] and payload["html"] == "<p>html body</p>"
    with pytest.raises(email.EmailError, match="500"):
        email.ResendBackend().send(_message())


def test_background_delivery_retries_and_masks_the_address(monkeypatch, caplog):
    monkeypatch.setattr(email, "RETRY_DELAYS_S", (0.0, 0.0))
    done = threading.Event()
    attempts = []

    class Flaky:
        synchronous = False

        def send(self, message):
            attempts.append(message.to)
            if len(attempts) < 3:
                raise OSError("connection reset")
            done.set()

    monkeypatch.setattr(email, "get_backend", lambda: Flaky())
    with caplog.at_level(logging.INFO, logger="services.email"):
        email.send(_message(to="adalovelace@example.com"))
        assert done.wait(5)
        email._pool().submit(lambda: None).result(5)  # the success log line is written
    assert len(attempts) == 3
    assert "a***@example.com" in caplog.text and "adalovelace@example.com" not in caplog.text


def test_a_broken_mail_setup_never_breaks_sign_up(monkeypatch, caplog, product):
    monkeypatch.setenv("EMAIL_BACKEND", "carrier-pigeon")
    get_settings.cache_clear()
    with caplog.at_level(logging.ERROR, logger="services.email"):
        assert signup(new_client()).status_code == 201
    assert "EMAIL_BACKEND must be one of" in caplog.text


def test_job_finished_subjects():
    notifications.send_job_finished("ada@example.com", "Ada Lovelace", job_id="j1", title="Sync", status="done",
                                    outputs=["final.mp4"])
    notifications.send_job_finished("ada@example.com", "Ada", job_id="j1", title="Sync", status="failed",
                                    error_code="timeout", error="too slow", retryable=True)
    subjects = [m.subject for m in email.outbox()]
    assert subjects == ["Your meeting “Sync” is ready", "Processing “Sync” failed"]
    assert "http://testserver/app/jobs/j1" in email.outbox()[0].text
    assert "Hi Ada," in email.outbox()[0].text


def test_cli_sends_a_test_message(capsys):
    assert email.main(["--test", "ops@example.com"]) == 0
    assert email.outbox()[-1].to == "ops@example.com"
    assert "sent" in capsys.readouterr().out
