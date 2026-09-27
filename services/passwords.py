"""Password hashing (argon2id) and the password policy (plan.MD §5.5).

Policy: 10-128 characters, not the email's local part, not a well-known
password. No composition rules (they push people to predictable patterns)."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 10
MAX_LENGTH = 128

# The most common passwords of 10+ characters (breach corpora) plus product
# words. Lower-cased comparison.
COMMON_PASSWORDS = frozenset({
    "1234567890", "12345678910", "123456789a", "0123456789", "1111111111", "0000000000", "1234512345",
    "qwertyuiop", "1q2w3e4r5t", "1qaz2wsx3edc", "qwerty1234", "qwerty12345", "asdfghjkl1", "zxcvbnm123",
    "password12", "password123", "password1234", "passw0rd123", "p@ssw0rd123", "password!1", "iloveyou12",
    "letmein123", "welcome123", "welcome1234", "admin12345", "administrator", "changeme123", "trustno1234",
    "football123", "baseball123", "superman123", "sunshine123", "princess123", "starwars123", "dragon12345",
    "monkey12345", "abcdefghij", "abc1234567", "aaaaaaaaaa", "abcd123456", "qazwsxedcr", "michael123",
    "computer123", "internet123", "whatever123", "secret1234", "123qweasdzxc", "1234qwerty", "mypassword1",
    "highlightcutter", "highlight123", "flolabs123", "flolabs1234", "meeting123", "zoom123456", "youtube123",
})

_hasher = PasswordHasher()  # argon2id, library defaults (time 3, memory 64 MiB, 4 lanes)


def check_policy(password: str, email: str = "") -> str | None:
    """The reason a password is refused, or None when it is acceptable."""
    if len(password) < MIN_LENGTH:
        return f"use at least {MIN_LENGTH} characters"
    if len(password) > MAX_LENGTH:
        return f"use at most {MAX_LENGTH} characters"
    if not password.strip():
        return "the password can't be only spaces"
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS:
        return "this password is too common; pick something less guessable"
    local = email.split("@", 1)[0].lower() if email else ""
    if local and len(local) >= 3 and lowered in (local, local + "123", local + "1234"):
        return "the password can't be your email address"
    if len(set(password)) <= 2:
        return "the password is too repetitive"
    return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# Verified against when the email is unknown, so a login attempt takes the
# same time whether or not the account exists.
_DUMMY_HASH: str | None = None


def burn_time(password: str) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _hasher.hash("dummy password for timing")
    verify_password(_DUMMY_HASH, password)


def use_fast_hasher_for_tests() -> None:
    """Tests hash hundreds of passwords; the production cost would add
    minutes. Never called by the app."""
    global _hasher, _DUMMY_HASH
    _hasher = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
    _DUMMY_HASH = None
