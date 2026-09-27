"""The one exception type services raise for expected failures. The API turns
it into the standard error body (api/errors.py):
{"error_code", "detail", "retryable", "retry_after_s"}."""

from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, message: str, *, code: str = "bad_request", status: int = 400,
                 retryable: bool = False, retry_after_s: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.retryable = retryable
        self.retry_after_s = retry_after_s


def invalid_credentials() -> ServiceError:
    # the same answer for an unknown email and a wrong password
    return ServiceError("wrong email or password", code="invalid_credentials", status=401)


def not_found(what: str = "not found") -> ServiceError:
    return ServiceError(what, code="not_found", status=404)


def rate_limited(retry_after_s: int) -> ServiceError:
    return ServiceError("too many requests; try again later", code="rate_limited", status=429,
                        retryable=True, retry_after_s=max(1, int(retry_after_s)))
