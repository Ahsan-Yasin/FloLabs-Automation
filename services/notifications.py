"""The emails the product sends (plan.MD §6.2). Each call renders a template
pair and hands it to services.email (never raises)."""

from __future__ import annotations

from core.config import get_settings

from . import email


def _link(path: str) -> str:
    return f"{get_settings().base_url}{path}"


def _first_name(name: str) -> str:
    return (name or "").split(" ")[0]


def send_verification(to: str, name: str, token: str) -> None:
    email.send_template(to, "verify_email", f"Verify your {get_settings().app_name} email",
                        name=_first_name(name), link=_link(f"/verify?token={token}"), hours=24)


def send_welcome(to: str, name: str) -> None:
    email.send_template(to, "welcome", f"Welcome to {get_settings().app_name}", name=_first_name(name),
                        app_link=_link("/app"), keys_link=_link("/app/keys"), docs_link=_link("/docs"))


def send_reset(to: str, name: str, token: str) -> None:
    email.send_template(to, "reset_password", "Reset your password", name=_first_name(name),
                        link=_link(f"/reset?token={token}"), minutes=60)


def send_password_changed(to: str, name: str) -> None:
    email.send_template(to, "password_changed", "Your password was changed", name=_first_name(name),
                        forgot_link=_link("/forgot"))


def send_job_finished(to: str, name: str, *, job_id: str, title: str, status: str, error_code: str | None = None,
                      error: str | None = None, retryable: bool = False, outputs: list[str] | None = None) -> None:
    title = title or "your meeting"
    if status == "done":
        subject = f"Your meeting “{title}” is ready"
    elif status == "decided":
        subject = f"The AI's picks for “{title}” are ready to review"
    else:
        subject = f"Processing “{title}” failed"
    email.send_template(to, "job_finished", subject, name=_first_name(name), title=title, status=status,
                        error_code=error_code or "", error=(error or "")[:400], retryable=retryable,
                        outputs=outputs or [], link=_link(f"/app/jobs/{job_id}"))
