"""Email delivery (plan.MD §6).

Backends (EMAIL_BACKEND):
  console  prints the message to the server log instead of sending (dev)
  memory   keeps messages in a list (tests: services.email.outbox())
  smtp     any SMTP provider (Amazon SES, Resend, Brevo, Postmark, Gmail...)
  resend   Resend's HTTP API

Messages are rendered from Jinja templates in web/templates/email/ (an HTML
and a plain-text twin each). SMTP and Resend deliveries run on a small
background pool with retries, so a request never waits for the mail server;
failures are logged with the address masked, never with the body (it can
contain one-time links).

Test your configuration:  python -m services.email --test you@example.com
"""

from __future__ import annotations

import argparse
import smtplib
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from core.config import get_settings
from core.logging import get_logger

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web" / "templates" / "email"
RETRY_DELAYS_S = (2.0, 10.0)
RESEND_URL = "https://api.resend.com/emails"


@dataclass
class OutgoingEmail:
    to: str
    subject: str
    text: str
    html: str
    tags: dict[str, str] = field(default_factory=dict)


class EmailError(RuntimeError):
    pass


class EmailRejected(EmailError):
    """A refusal that retrying can't fix: a wrong SMTP login, a bad API key."""


def mask(address: str) -> str:
    local, _, domain = (address or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _sender() -> str:
    settings = get_settings()
    return settings.email_from or settings.smtp_sender_fallback or f"{settings.app_name} <no-reply@localhost>"


# ------------------------------------------------------------------ backends
class ConsoleBackend:
    synchronous = True

    def send(self, message: OutgoingEmail) -> None:
        logger.info("email (EMAIL_BACKEND=console, not delivered)\nTo: %s\nSubject: %s\n\n%s",
                    message.to, message.subject, message.text)


_outbox: list[OutgoingEmail] = []
_outbox_lock = threading.Lock()


class MemoryBackend:
    synchronous = True

    def send(self, message: OutgoingEmail) -> None:
        with _outbox_lock:
            _outbox.append(message)


class SmtpBackend:
    synchronous = False

    def send(self, message: OutgoingEmail) -> None:
        settings = get_settings()
        if not settings.smtp_host:
            raise EmailError("SMTP_HOST is empty")
        mime = EmailMessage()
        mime["From"] = _sender()
        mime["To"] = message.to
        mime["Subject"] = message.subject
        if settings.email_reply_to:
            mime["Reply-To"] = settings.email_reply_to
        domain = parseaddr(_sender())[1].rpartition("@")[2] or "localhost"
        mime["Message-ID"] = make_msgid(domain=domain)
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")
        mode = settings.smtp_tls.strip().lower()
        context = ssl.create_default_context()
        timeout = settings.email_timeout_s
        if mode == "ssl":
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout, context=context)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
        with server:
            server.ehlo()
            if mode == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if settings.smtp_user:
                _login(server, settings.smtp_user, settings.smtp_password)
            server.send_message(mime)


def _login(server: smtplib.SMTP, user: str, password: str) -> None:
    """Sign in, with AUTH LOGIN when the server offers it. Gmail answers a
    wrong password on AUTH PLAIN (smtplib's first choice) by dropping the
    connection, which reads like a network fault; on AUTH LOGIN it says
    "Username and Password not accepted"."""
    try:
        if "LOGIN" in getattr(server, "esmtp_features", {}).get("auth", "").upper().split():
            server.user, server.password = user, password
            server.auth("LOGIN", server.auth_login, initial_response_ok=False)
        else:
            server.login(user, password)
    except smtplib.SMTPAuthenticationError as exc:
        reply = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        raise EmailRejected(
            f"the mail server refused SMTP_USER/SMTP_PASSWORD ({exc.smtp_code} {' '.join(reply.split())[:140]}). "
            "For Gmail, SMTP_PASSWORD must be an app password of SMTP_USER: "
            "https://myaccount.google.com/apppasswords") from exc


class ResendBackend:
    synchronous = False

    def send(self, message: OutgoingEmail) -> None:
        settings = get_settings()
        if not settings.resend_api_key:
            raise EmailError("RESEND_API_KEY is empty")
        payload = {"from": _sender(), "to": [message.to], "subject": message.subject, "text": message.text,
                   "html": message.html}
        if settings.email_reply_to:
            payload["reply_to"] = settings.email_reply_to
        response = httpx.post(RESEND_URL, json=payload, timeout=settings.email_timeout_s,
                              headers={"Authorization": f"Bearer {settings.resend_api_key}"})
        if response.status_code in (401, 403, 422):  # bad key, unverified sender: retrying won't help
            raise EmailRejected(f"Resend answered {response.status_code}: {response.text[:200]}")
        if response.status_code >= 300:
            raise EmailError(f"Resend answered {response.status_code}: {response.text[:200]}")


BACKENDS = {"console": ConsoleBackend, "memory": MemoryBackend, "smtp": SmtpBackend, "resend": ResendBackend}


def get_backend():
    name = get_settings().email_backend.strip().lower()
    backend = BACKENDS.get(name)
    if backend is None:
        raise EmailError(f"EMAIL_BACKEND must be one of {', '.join(BACKENDS)} (got {name!r})")
    return backend()


# ------------------------------------------------------------------ sending
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="email")
        return _executor


def _attempt(backend, message: OutgoingEmail, attempts: int) -> Exception | None:
    """Send with up to `attempts` tries; None when it went out, else the last
    error (logged). A refusal (EmailRejected) is not tried again."""
    kind = message.tags.get("kind", "email")
    error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            backend.send(message)
        except EmailRejected as exc:
            logger.error("%s to %s not sent: %s", kind, mask(message.to), exc)
            return exc
        except Exception as exc:  # noqa: BLE001 — logged; mail must never break a request or a job
            logger.warning("%s to %s failed (attempt %d/%d): %s", kind, mask(message.to), attempt, attempts, exc)
            error = exc
            if attempt < attempts:
                time.sleep(RETRY_DELAYS_S[min(attempt - 1, len(RETRY_DELAYS_S) - 1)])
            continue
        logger.info("%s sent to %s", kind, mask(message.to))
        return None
    return error


def _deliver(backend, message: OutgoingEmail, attempts: int) -> bool:
    return _attempt(backend, message, attempts) is None


def _clean(message: OutgoingEmail) -> OutgoingEmail:
    message.subject = " ".join(message.subject.split())[:200]  # no header injection, no runaway subjects
    return message


def send(message: OutgoingEmail) -> None:
    """Queue a message (or deliver it now with the console/memory backends).
    Never raises: a mail problem must not fail a sign-up or a job."""
    _clean(message)
    try:
        backend = get_backend()
    except EmailError as exc:
        logger.error("email not sent: %s", exc)
        return
    if backend.synchronous:
        _deliver(backend, message, attempts=1)
    else:
        _pool().submit(_deliver, backend, message, 1 + len(RETRY_DELAYS_S))


def send_now(message: OutgoingEmail) -> str | None:
    """Deliver once, right away, for someone waiting on the answer (the
    "resend the email" button). None when it went out; otherwise why not, in
    a sentence for that person (the log line has the details). Never raises."""
    _clean(message)
    try:
        backend = get_backend()
    except EmailError as exc:
        logger.error("email not sent: %s", exc)
        return "this site's email isn't set up correctly"
    error = _attempt(backend, message, attempts=1)
    if error is None:
        return None
    if isinstance(error, EmailRejected):
        return "the mail server refused this site's sign-in, so the site's email settings need fixing"
    return "the mail server didn't answer. Try again in a minute"


# ------------------------------------------------------------------ templates
def _autoescape(name: str | None) -> bool:
    return bool(name) and name.endswith(".html.j2")


# autoescape is on for every .html.j2 (the callable above); only the plain-text
# twins are unescaped, on purpose. Bandit can't see through the callable.
_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=_autoescape,  # nosec B701
                   undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)


def render(template: str, **context) -> tuple[str, str]:
    """(text, html) of web/templates/email/<template>.txt.j2 / .html.j2."""
    settings = get_settings()
    base = {
        "app_name": settings.app_name,
        "base_url": settings.base_url,
        "contact_email": settings.contact_email,
        "year": datetime.now(UTC).year,
    }
    text = _env.get_template(f"{template}.txt.j2").render(**base, **context).strip() + "\n"
    html = _env.get_template(f"{template}.html.j2").render(**base, **context)
    return text, html


def send_template(to: str, template: str, subject: str, **context) -> None:
    try:
        text, html = render(template, **context)
    except Exception:
        logger.exception("could not render email template %s", template)
        return
    send(OutgoingEmail(to=to, subject=subject, text=text, html=html, tags={"kind": template}))


def send_template_now(to: str, template: str, subject: str, **context) -> str | None:
    """send_template, delivered right away; see send_now()."""
    try:
        text, html = render(template, **context)
    except Exception:
        logger.exception("could not render email template %s", template)
        return "this site's email isn't set up correctly"
    return send_now(OutgoingEmail(to=to, subject=subject, text=text, html=html, tags={"kind": template}))


# ------------------------------------------------------------------ tests / CLI
def outbox() -> list[OutgoingEmail]:
    with _outbox_lock:
        return list(_outbox)


def reset_for_tests() -> None:
    with _outbox_lock:
        _outbox.clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m services.email", description="Send a test email")
    parser.add_argument("--test", metavar="ADDRESS", required=True, help="where to send the test message")
    args = parser.parse_args(argv)
    settings = get_settings()
    text, html = render("test", backend=settings.email_backend)
    message = OutgoingEmail(to=args.test, subject=f"{settings.app_name}: test email", text=text, html=html,
                            tags={"kind": "test"})
    backend = get_backend()
    print(f"sending through EMAIL_BACKEND={settings.email_backend} to {args.test} ...")
    ok = _deliver(backend, message, attempts=1)
    print("sent" if ok else "FAILED (see the log line above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
