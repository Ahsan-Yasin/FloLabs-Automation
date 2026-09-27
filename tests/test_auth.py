"""Accounts, sessions and principals (plan.MD P1)."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from sqlalchemy import select

import api.main as api_main
from api.ratelimit import limiter
from core.config import get_settings
from db.models import RefreshToken, User
from db.session import session_scope
from services import passwords, tokens
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
    token_in,
    verify,
)

pytestmark = pytest.mark.usefixtures("product")


@pytest.fixture(autouse=True)
def _queue_drained():
    yield
    assert api_main.job_queue.wait_idle(10), "a test left a job running"


def _user(email_address="ada@example.com") -> User:
    with session_scope() as db:
        return db.scalar(select(User).where(User.email == email_address))


# ------------------------------------------------------------------ sign-up
def test_signup_signs_in_and_sends_a_verification_link():
    client = new_client()
    response = signup(client, "  Ada@Example.com ")
    assert response.status_code == 201
    body = response.json()
    assert body["verification_sent"] is True and body["token_type"] == "bearer"
    assert body["user"]["email"] == "ada@example.com" and body["user"]["email_verified"] is False
    assert {"hc_access", "hc_refresh"} <= set(client.cookies.keys())
    set_cookies = response.headers.get_list("set-cookie")
    assert all("httponly" in c.lower() and "samesite=lax" in c.lower() for c in set_cookies)

    message = emails_to("ada@example.com", "verify_email")[-1]
    assert message.subject.startswith("Verify your")
    assert "http://testserver/verify?token=" in message.text and "/verify?token=" in message.html
    assert "{{" not in message.html and "{{" not in message.text

    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["name"] == "Ada Lovelace"
    assert new_client().get("/api/v1/auth/me", headers=bearer(body["access_token"])).status_code == 200


@pytest.mark.parametrize(("email_address", "password", "status", "code"), [
    ("ada@example.com", "short", 422, "weak_password"),
    ("ada@example.com", "password123", 422, "weak_password"),
    ("ada@example.com", "ada@example", 201, None),
    ("not-an-email", PASSWORD, 422, "invalid_email"),
])
def test_signup_validation(email_address, password, status, code):
    response = signup(new_client(), email_address, password)
    assert response.status_code == status, response.text
    if code:
        assert response.json()["error_code"] == code


def test_duplicate_email_is_refused_case_insensitively():
    assert signup(new_client(), "ada@example.com").status_code == 201
    again = signup(new_client(), "ADA@example.COM")
    assert again.status_code == 409 and again.json()["error_code"] == "email_taken"


def test_signup_can_be_closed_or_limited_to_domains(monkeypatch):
    monkeypatch.setenv("SIGNUP_ENABLED", "false")
    get_settings.cache_clear()
    assert signup(new_client()).json()["error_code"] == "signup_closed"
    monkeypatch.setenv("SIGNUP_ENABLED", "true")
    monkeypatch.setenv("ALLOWED_SIGNUP_DOMAINS", "flolabs.io, @example.org")
    get_settings.cache_clear()
    assert signup(new_client(), "a@example.com").json()["error_code"] == "signup_domain_not_allowed"
    assert signup(new_client(), "a@example.org").status_code == 201
    assert signup(new_client(), "b@FloLabs.io").status_code == 201


def test_signup_rate_limit_counts_only_created_accounts():
    client = new_client()
    for _ in range(3):  # refused sign-ups (a weak password) don't use up the allowance
        assert signup(client, "weak@example.com", "short").status_code == 422
    for i in range(5):
        assert signup(client, f"user{i}@example.com").status_code == 201
    limited = signup(client, "six@example.com")
    assert limited.status_code == 429 and limited.json()["error_code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) > 0


# ------------------------------------------------------------------ verification
def test_verification_link_sends_one_welcome_email_and_is_scanner_safe():
    client = new_client()
    signup(client)
    first = verify(client)
    assert first.status_code == 200 and first.json() == {"verified": True, "already_verified": False,
                                                         "email": "ada@example.com"}
    assert len(emails_to("ada@example.com", "welcome")) == 1
    again = verify(client)  # a mail scanner opened the link first; the person clicks it later
    assert again.status_code == 200 and again.json()["already_verified"] is True
    assert len(emails_to("ada@example.com", "welcome")) == 1
    assert client.get("/api/v1/auth/me").json()["email_verified"] is True


def test_bad_or_expired_verification_tokens():
    client = new_client()
    signup(client)
    bad = client.post("/api/v1/auth/verify", json={"token": "nope"}, headers=CSRF)
    assert bad.status_code == 400 and bad.json()["error_code"] == "token_invalid"
    with session_scope() as db:
        from db.models import EmailToken

        for token in db.scalars(select(EmailToken)):
            token.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    assert verify(client).json()["error_code"] == "token_invalid"


def test_resend_verification_replaces_the_old_link_and_is_limited():
    client = new_client()
    signup(client)
    old = token_in(emails_to("ada@example.com", "verify_email")[-1])
    assert client.post("/api/v1/auth/resend-verification", headers=CSRF).json() == {"ok": True}
    assert client.post("/api/v1/auth/verify", json={"token": old}, headers=CSRF).json()["error_code"] ==         "token_invalid"
    assert verify(client).status_code == 200
    again = client.post("/api/v1/auth/resend-verification", headers=CSRF)
    assert again.status_code == 409 and again.json()["error_code"] == "already_verified"

    other = new_client()
    signup(other, "bob@example.com")
    codes = [other.post("/api/v1/auth/resend-verification", headers=CSRF).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_admin_emails_become_admins_only_once_verified(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    get_settings.cache_clear()
    client = new_client()
    assert signup(client, "boss@example.com").json()["user"]["role"] == "user"  # anyone could claim the address
    verify(client, "boss@example.com")
    assert client.get("/api/v1/auth/me").json()["is_admin"] is True


# ------------------------------------------------------------------ login
def test_login_success_and_uniform_failures():
    signup(new_client())
    client = new_client()
    ok = login(client)
    assert ok.status_code == 200 and ok.json()["user"]["email"] == "ada@example.com"
    assert _user().last_login_at is not None
    wrong = login(new_client(), password="wrong password!")
    unknown = login(new_client(), "nobody@example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["error_code"] == "invalid_credentials"
    assert wrong.headers["WWW-Authenticate"] == "Bearer"


def test_failed_logins_lock_the_account_for_a_while():
    signup(new_client())
    for _ in range(8):
        assert login(new_client(), password="wrong password!").status_code == 401
    locked = login(new_client())  # even the right password waits now
    assert locked.status_code == 429 and locked.json()["retryable"] is True
    limiter.clock = lambda: 10**9  # ...until the window has passed
    assert login(new_client()).status_code == 200


def test_disabled_accounts_cannot_log_in_and_lose_their_sessions(monkeypatch):
    client = signed_in()
    from db.cli import main as cli

    monkeypatch.setenv("HC_NEW_PASSWORD", "unused-password-1")
    assert cli(["deactivate", "ada@example.com"]) == 0
    assert client.get("/api/v1/auth/me").status_code == 401
    assert login(new_client()).json()["error_code"] == "account_disabled"


# ------------------------------------------------------------------ sessions
def test_refresh_rotates_the_refresh_token():
    client = signed_in()
    first = client.cookies.get("hc_refresh")
    response = client.post("/api/v1/auth/refresh", headers=CSRF)
    assert response.status_code == 200 and response.json()["access_token"]
    assert client.cookies.get("hc_refresh") != first
    assert client.get("/api/v1/auth/me").status_code == 200


def test_replaying_an_old_refresh_token_revokes_the_whole_session():
    client = signed_in()
    stolen = client.cookies.get("hc_refresh")
    assert client.post("/api/v1/auth/refresh", headers=CSRF).status_code == 200
    # a parallel refresh right away is tolerated (two tabs refreshing at once)
    assert new_client().post("/api/v1/auth/refresh", json={"refresh_token": stolen}).status_code == 200
    with session_scope() as db:  # ...but not once the grace window has passed
        for token in db.scalars(select(RefreshToken).where(RefreshToken.replaced_at.is_not(None))):
            token.replaced_at = datetime.now(UTC) - timedelta(minutes=5)
    replay = new_client().post("/api/v1/auth/refresh", json={"refresh_token": stolen})
    assert replay.status_code == 401 and replay.json()["error_code"] == "session_revoked"
    # the legitimate browser's newer token died with the family
    after = client.post("/api/v1/auth/refresh", headers=CSRF)
    assert after.status_code == 401 and "hc_refresh" not in client.cookies


def test_logout_revokes_the_session_and_clears_cookies():
    client = signed_in()
    refresh = client.cookies.get("hc_refresh")
    assert client.post("/api/v1/auth/logout", headers=CSRF).json() == {"ok": True}
    assert "hc_access" not in client.cookies and "hc_refresh" not in client.cookies
    dead = new_client().post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert dead.status_code == 401


def test_logout_everywhere_kills_sessions_and_older_access_tokens():
    client = signed_in()
    other = new_client()
    login(other)
    user = _user()
    past = datetime.now(UTC) - timedelta(seconds=30)
    old_token = jwt.encode({"sub": user.id, "type": "access", "iat": past, "exp": past + timedelta(minutes=15)},
                           tokens.jwt_secret(), algorithm="HS256")
    assert new_client().get("/api/v1/auth/me", headers=bearer(old_token)).status_code == 200
    assert client.post("/api/v1/auth/logout-all", headers=CSRF).status_code == 200
    assert new_client().get("/api/v1/auth/me", headers=bearer(old_token)).status_code == 401
    assert other.post("/api/v1/auth/refresh", headers=CSRF).status_code == 401


def test_sessions_can_be_listed_and_revoked_one_by_one():
    client = signed_in()
    laptop = new_client()
    login(laptop)
    items = client.get("/api/v1/auth/sessions").json()["items"]
    assert len(items) == 2 and sum(item["current"] for item in items) == 1
    other = next(item for item in items if not item["current"])
    assert client.delete(f"/api/v1/auth/sessions/{other['id']}", headers=CSRF).status_code == 200
    assert laptop.post("/api/v1/auth/refresh", headers=CSRF).status_code == 401
    assert client.delete("/api/v1/auth/sessions/nope", headers=CSRF).status_code == 404


def test_expired_access_cookie_still_works_for_reads_through_the_refresh_cookie():
    client = signed_in()
    client.cookies.set("hc_access", "expired.or.garbage")
    assert client.get("/api/v1/auth/me").status_code == 200  # e.g. a <video> range request
    assert client.patch("/api/v1/auth/me", json={"name": "x"}, headers=CSRF).status_code == 401  # writes refresh first


# ------------------------------------------------------------------ passwords
def test_change_password_keeps_this_browser_and_signs_out_the_rest():
    client = signed_in()
    other = new_client()
    login(other)
    wrong = client.post("/api/v1/auth/change-password", headers=CSRF,
                        json={"current_password": "nope nope nope", "new_password": "another good one"})
    assert wrong.status_code == 403
    weak = client.post("/api/v1/auth/change-password", headers=CSRF,
                       json={"current_password": PASSWORD, "new_password": "short"})
    assert weak.json()["error_code"] == "weak_password"
    ok = client.post("/api/v1/auth/change-password", headers=CSRF,
                     json={"current_password": PASSWORD, "new_password": "another good one"})
    assert ok.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200
    assert other.post("/api/v1/auth/refresh", headers=CSRF).status_code == 401
    assert login(new_client()).status_code == 401
    assert login(new_client(), password="another good one").status_code == 200
    assert emails_to("ada@example.com", "password_changed")


def test_forgot_and_reset_password():
    client = signed_in(verified=False)
    unknown = new_client().post("/api/v1/auth/forgot", json={"email": "nobody@example.com"})
    assert unknown.json() == {"ok": True} and not emails_to("nobody@example.com")
    assert new_client().post("/api/v1/auth/forgot", json={"email": "ADA@example.com"}).json() == {"ok": True}
    token = token_in(emails_to("ada@example.com", "reset_password")[-1])

    weak = new_client().post("/api/v1/auth/reset", json={"token": token, "new_password": "short"})
    assert weak.json()["error_code"] == "weak_password"
    done = new_client().post("/api/v1/auth/reset", json={"token": token, "new_password": "brand new secret"})
    assert done.status_code == 200
    again = new_client().post("/api/v1/auth/reset", json={"token": token, "new_password": "brand new secret 2"})
    assert again.json()["error_code"] == "token_invalid"
    assert client.post("/api/v1/auth/refresh", headers=CSRF).status_code == 401  # all sessions ended
    fresh = new_client()
    assert login(fresh, password="brand new secret").status_code == 200
    assert fresh.get("/api/v1/auth/me").json()["email_verified"] is True  # the link proved the inbox


def test_reset_emails_are_limited_per_address():
    signup(new_client())
    for _ in range(5):
        assert new_client().post("/api/v1/auth/forgot", json={"email": "ada@example.com"}).status_code == 200
    assert len(emails_to("ada@example.com", "reset_password")) == 3


# ------------------------------------------------------------------ profile, secret
def test_profile_updates():
    client = signed_in()
    updated = client.patch("/api/v1/auth/me", headers=CSRF, json={"name": "  Ada   King ", "notify_on_done": False})
    body = updated.json()
    assert body["name"] == "Ada King" and body["notify_on_done"] is False
    # Zoom was removed: an old client still sending its field is ignored, not refused
    old_client = client.patch("/api/v1/auth/me", headers=CSRF, json={"zoom_host_email": "host@example.com"})
    assert old_client.status_code == 200 and "zoom_host_email" not in old_client.json()


def test_webhook_secret_reveal_and_rotate():
    client = signed_in()
    secret = client.get("/api/v1/auth/webhook-secret").json()["webhook_secret"]
    assert secret.startswith("whsec_")
    assert client.get("/api/v1/auth/me").json()["webhook_secret_hint"].endswith(secret[-4:])
    rotated = client.post("/api/v1/auth/webhook-secret/rotate", headers=CSRF).json()["webhook_secret"]
    assert rotated != secret and client.get("/api/v1/auth/webhook-secret").json()["webhook_secret"] == rotated


# ------------------------------------------------------------------ principals / middleware
def test_api_paths_need_credentials_when_the_dev_mode_is_off():
    anonymous = new_client().get("/jobs")
    assert anonymous.status_code == 401 and anonymous.headers["WWW-Authenticate"] == "Bearer"
    assert anonymous.json()["error_code"] == "unauthorized"
    assert new_client().get("/api/v1/auth/me").status_code == 401
    assert new_client().get("/health").status_code == 200


def test_cookie_sessions_need_the_csrf_header_for_writes():
    client = signed_in()
    blocked = client.patch("/api/v1/auth/me", json={"name": "x"})
    assert blocked.status_code == 403 and blocked.json()["error_code"] == "csrf"
    assert client.patch("/api/v1/auth/me", json={"name": "x"}, headers=CSRF).status_code == 200
    token = login(new_client()).json()["access_token"]
    assert new_client().patch("/api/v1/auth/me", json={"name": "y"}, headers=bearer(token)).status_code == 200


def test_api_keys_authenticate_by_header_and_respect_revocation_and_expiry():
    user_id = make_user("ada@example.com")
    key = make_key(user_id)
    for headers in (bearer(key), {"X-API-Key": key}):
        assert new_client().get("/jobs", headers=headers).status_code == 200
    # keys can't manage the account
    refused = new_client().get("/api/v1/auth/me", headers=bearer(key))
    assert refused.status_code == 403 and refused.json()["error_code"] == "session_required"
    tampered = key[:-1] + ("A" if key[-1] != "A" else "B")
    assert new_client().get("/jobs", headers=bearer(tampered)).status_code == 401
    with session_scope() as db:
        from db.models import ApiKey

        db.scalars(select(ApiKey)).one().expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert new_client().get("/jobs", headers=bearer(key)).status_code == 401


def test_an_invalid_explicit_credential_never_falls_back_to_cookies():
    client = signed_in()
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.get("/api/v1/auth/me", headers=bearer("hc_live_nope")).status_code == 401


def test_operator_key_is_an_admin_without_a_user(monkeypatch):
    monkeypatch.setenv("HC_API_TOKEN", "ops-secret-value")
    get_settings.cache_clear()
    assert new_client().get("/jobs", headers={"X-API-Key": "ops-secret-value"}).status_code == 200
    assert new_client().get("/jobs", headers=bearer("ops-secret-value")).status_code == 200
    assert new_client().get("/api/v1/auth/me", headers={"X-API-Key": "ops-secret-value"}).status_code == 403


def test_api_key_requests_are_rate_limited():
    key = make_key(make_user("ada@example.com"))
    client = new_client()
    limiter.clock = lambda: 1000.0  # frozen: no refill between requests
    codes = [client.get("/jobs", headers=bearer(key)).status_code for _ in range(121)]
    assert codes[:120] == [200] * 120 and codes[120] == 429


# ------------------------------------------------------------------ units
def test_password_policy_and_hashing():
    assert passwords.check_policy("short") is not None
    assert passwords.check_policy("aaaaaaaaaaaa") is not None
    assert passwords.check_policy("1234567890") is not None
    assert passwords.check_policy("adalovelace", "adalovelace@example.com") is not None
    assert passwords.check_policy("x" * 129) is not None
    assert passwords.check_policy("tangerine trombone") is None
    digest = passwords.hash_password("tangerine trombone")
    assert digest.startswith("$argon2id$")
    assert passwords.verify_password(digest, "tangerine trombone")
    assert not passwords.verify_password(digest, "tangerine trombone!")
    assert not passwords.verify_password("not a hash", "x")


def test_access_tokens_reject_tampering_type_and_expiry():
    token, ttl = tokens.make_access_token("u1", "a@example.com", "user")
    assert ttl == 15 * 60 and tokens.decode_access_token(token)["sub"] == "u1"
    header, payload, signature = token.split(".")
    assert tokens.decode_access_token(f"{header}.{payload}.{signature[::-1]}") is None
    other = jwt.encode({"sub": "u1", "type": "refresh", "iat": datetime.now(UTC),
                        "exp": datetime.now(UTC) + timedelta(minutes=5)}, tokens.jwt_secret(), algorithm="HS256")
    assert tokens.decode_access_token(other) is None
    stale = jwt.encode({"sub": "u1", "type": "access", "iat": datetime.now(UTC) - timedelta(hours=1),
                        "exp": datetime.now(UTC) - timedelta(minutes=5)}, tokens.jwt_secret(), algorithm="HS256")
    assert tokens.decode_access_token(stale) is None
    none_alg = jwt.encode({"sub": "u1", "type": "access", "iat": 1, "exp": 4102444800}, key=None, algorithm="none")
    assert tokens.decode_access_token(none_alg) is None


def test_dev_secret_is_created_once_and_prod_requires_one(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "")
    get_settings.cache_clear()
    first = tokens.jwt_secret()
    assert len(first) >= 32 and tokens.jwt_secret() == first
    assert (get_settings().storage_dir / "dev_jwt_secret").read_text().strip() == first
    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        tokens.jwt_secret()


def test_rate_limiter_refills_over_time():
    from api.ratelimit import RateLimiter

    now = [0.0]
    bucket = RateLimiter(clock=lambda: now[0])
    assert all(bucket.hit("verify_resend", "u") is None for _ in range(3))
    wait = bucket.hit("verify_resend", "u")
    assert wait == 1200  # 3 per hour: one more every 20 minutes
    now[0] = 1200
    assert bucket.check("verify_resend", "u") is None and bucket.hit("verify_resend", "u") is None
    assert bucket.hit("verify_resend", "other") is None


def test_cli_admin_commands(monkeypatch, capsys):
    from db.cli import main as cli

    monkeypatch.setenv("HC_NEW_PASSWORD", "operator chosen pw")
    assert cli(["create-admin", "Root@Example.com", "--name", "Root"]) == 0
    assert cli(["create-admin", "root@example.com"]) == 1  # exists
    assert login(new_client(), "root@example.com", "operator chosen pw").json()["user"]["is_admin"] is True
    assert cli(["demote", "root@example.com"]) == 1  # the last admin
    make_user("bob@example.com")
    assert cli(["promote", "bob@example.com"]) == 0
    assert cli(["demote", "root@example.com"]) == 0
    assert cli(["promote", "nobody@example.com"]) == 1
    monkeypatch.setenv("HC_NEW_PASSWORD", "reset by operator")
    assert cli(["set-password", "bob@example.com"]) == 0
    assert login(new_client(), "bob@example.com", "reset by operator").status_code == 200
    assert "error" in capsys.readouterr().err
