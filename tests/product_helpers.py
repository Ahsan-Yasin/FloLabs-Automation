"""Helpers for the product-layer tests (accounts, keys, jobs ownership).
Use them together with the `product` fixture (tests/conftest.py)."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

import api.main as api_main
from db.models import User
from db.session import session_scope
from services import api_keys, email, users

PASSWORD = "correct horse battery"
CSRF = {"X-Requested-With": "hc-web"}
_TOKEN_RE = re.compile(r"token=([A-Za-z0-9_\-]+)")


def new_client() -> TestClient:
    return TestClient(api_main.app)


def signup(client: TestClient, email_address: str = "ada@example.com", password: str = PASSWORD,
           name: str = "Ada Lovelace"):
    # the site's fetch wrapper always sends the CSRF header (a client holding
    # session cookies from an earlier request needs it)
    return client.post("/api/v1/auth/signup", json={"email": email_address, "password": password, "name": name},
                       headers=CSRF)


def login(client: TestClient, email_address: str = "ada@example.com", password: str = PASSWORD):
    return client.post("/api/v1/auth/login", json={"email": email_address, "password": password}, headers=CSRF)


def emails_to(address: str, kind: str | None = None) -> list[email.OutgoingEmail]:
    return [m for m in email.outbox() if m.to == address and (kind is None or m.tags.get("kind") == kind)]


def token_in(message: email.OutgoingEmail) -> str:
    match = _TOKEN_RE.search(message.text)
    assert match, message.text
    return match.group(1)


def verify(client: TestClient, email_address: str = "ada@example.com"):
    message = emails_to(email_address, "verify_email")[-1]
    return client.post("/api/v1/auth/verify", json={"token": token_in(message)}, headers=CSRF)


def signed_in(email_address: str = "ada@example.com", *, verified: bool = True, name: str = "Ada Lovelace",
              password: str = PASSWORD) -> TestClient:
    """A client with a browser session (cookies) for a new account."""
    client = new_client()
    response = signup(client, email_address, password, name)
    assert response.status_code == 201, response.text
    if verified:
        assert verify(client, email_address).status_code == 200
    return client


def make_user(email_address: str, *, verified: bool = True, role: str = "user", password: str = PASSWORD) -> str:
    """Create an account straight in the database; returns its id."""
    with session_scope() as db:
        user = users.signup(db, email_address, password, email_address.split("@")[0])
        if verified:
            users.verify_email(db, users.issue_email_token(db, user, "verify"))
        user.role = role
        db.flush()
        return user.id


def make_key(user_id: str, scopes: list[str] | None = None, name: str = "test key") -> str:
    with session_scope() as db:
        user = db.get(User, user_id)
        _, full = api_keys.create(db, user, name, scopes)
        return full


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
