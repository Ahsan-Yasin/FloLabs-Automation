"""OpenAI calls for the decide stage (the default LLM provider).

`OpenAICaller` has the same interface as `GeminiCaller` (model, usage,
limiter, deadline, stage, start_stage, generate), so judge_segments,
rerank_moments and generate_chapters work unchanged with either provider.

It talks to the Responses API with plain httpx (no SDK): one POST per call,
`store: false` (meeting transcripts are not kept on OpenAI's side for later
retrieval), reasoning off by default (thinking tokens are billed as output and
the compact answer format doesn't need them). Error handling mirrors Gemini's:
an account without credit fails fast with `llm_quota_exhausted` (retrying
can't help until someone adds billing), other 429s wait for the time the
server asks for, 408/409/5xx and network errors are retried with backoff, and
a bad key / model / request (or one that can't be sent at all) fails the job
as not retryable. Error text is redacted: it reaches job.json, job.log and the UI.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import httpx

from core.errors import PipelineError
from core.logging import get_logger

from .gemini_client import (
    DEFAULT_429_WAIT_S,
    MAX_429_WAIT_S,
    MAX_RATE_LIMIT_RETRIES,
    MAX_SERVER_ERROR_RETRIES,
    DecisionError,
    LLMUsage,
    RateLimiter,
)

logger = get_logger(__name__)

# Errors that retrying the same request can never fix (bad request, bad key,
# no access to the model, unknown model, invalid parameters).
PERMANENT_HTTP_CODES = {400, 401, 403, 404, 422}
# Request timeout / conflict: OpenAI documents both as safe to retry.
RETRYABLE_HTTP_CODES = {408, 409}
# A 429 can say "wait 120ms"; re-sending that fast just burns the retries.
MIN_429_WAIT_S = 1.0
QUOTA_MESSAGE = (
    "the OpenAI account has no credit left (insufficient_quota) — add billing or credits at "
    "platform.openai.com (Settings > Billing), then retry the job"
)


@dataclass
class OpenAIResponse:
    """What the decide code reads from an answer (the same .text the Gemini
    response has, plus finish_reason in Gemini's MAX_TOKENS vocabulary)."""

    text: str
    finish_reason: str
    usage: dict = field(default_factory=dict)


# ------------------------------------------------------------------ transport


def request_body(model: str, system_prompt: str, contents: str, schema: dict | None = None, *,
                 max_output_tokens: int, reasoning_effort: str = "") -> dict:
    body: dict = {
        "model": model,
        "instructions": system_prompt,
        "input": contents,
        "store": False,
        "max_output_tokens": max_output_tokens,
    }
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    if schema is not None:
        # strict mode rejects schemas without additionalProperties=false on
        # every object; the decide prompts use plain text anyway
        body["text"] = {"format": {"type": "json_schema", "name": "answer", "schema": schema, "strict": False}}
    return body


def _call_openai(client: httpx.Client, body: dict) -> httpx.Response:
    """The single network call (tests replace this)."""
    return client.post("/responses", json=body)


def parse_response(data: dict) -> OpenAIResponse:
    """Join every output_text part of every message item (reasoning items
    carry no answer). A refusal yields empty text, which the callers already
    treat as an invalid answer (retry, then split)."""
    texts: list[str] = []
    refused = False
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                texts.append(part.get("text") or "")
            elif part.get("type") == "refusal":
                refused = True
                logger.warning("openai refused to answer: %s", str(part.get("refusal", ""))[:200])
    status = str(data.get("status") or "unknown")
    reason = (data.get("incomplete_details") or {}).get("reason")
    finish = "MAX_TOKENS" if status == "incomplete" and reason == "max_output_tokens" else status
    if status == "failed":
        logger.warning("openai response failed: %s", data.get("error"))
    return OpenAIResponse("" if refused else "".join(texts), finish, data.get("usage") or {})


# ------------------------------------------------------------------ errors


def _error_info(response: httpx.Response) -> tuple[str, str]:
    """(error code or type, message) from an OpenAI error body."""
    try:
        err = response.json().get("error") or {}
    except (ValueError, AttributeError):
        return "", response.text[:300]
    if not isinstance(err, dict):
        return "", str(err)[:300]
    kind = str(err.get("code") or err.get("type") or "")
    if err.get("type") == "insufficient_quota":
        kind = "insufficient_quota"
    return kind, str(err.get("message") or "")


_DURATION_PART = re.compile(r"([\d.]+)(ms|h|m|s)")
_TRY_AGAIN = re.compile(r"try again in\s+((?:[\d.]+(?:ms|h|m|s))+)", re.IGNORECASE)


def parse_reset(value: str | None) -> float | None:
    """OpenAI's reset durations: "1s", "6m0s", "120ms", "1h2m3.5s" (or a
    bare number of seconds) -> seconds."""
    if value is None:
        return None
    text = str(value).strip().lower()
    try:
        return float(text)
    except ValueError:
        pass
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    scale = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return sum(float(n) * scale[u] for n, u in parts)


def retry_wait(response: httpx.Response, message: str) -> float | None:
    """How long the server asked us to wait before retrying a 429, if it said."""
    headers = response.headers
    try:
        if headers.get("retry-after-ms") is not None:
            return float(headers["retry-after-ms"]) / 1000.0
    except ValueError:
        pass
    try:
        if headers.get("retry-after") is not None:
            return float(headers["retry-after"])
    except ValueError:
        pass  # an HTTP date: fall through to the other hints
    # which limit was hit isn't said; waiting for the later reset is safe
    resets = [parse_reset(headers.get(f"x-ratelimit-reset-{kind}")) for kind in ("requests", "tokens")]
    resets = [r for r in resets if r is not None]
    if resets:
        return max(resets)
    m = _TRY_AGAIN.search(message or "")
    return parse_reset(m.group(1)) if m else None


def _redact(message: str, api_key: str) -> str:
    """Error messages end up in job.json and the UI; OpenAI's 401 text quotes
    the (masked) key, so drop anything key-shaped."""
    if api_key:
        message = message.replace(api_key, "sk-***")
    return re.sub(r"sk-[A-Za-z0-9_\-*]{4,}", "sk-***", message)


# ------------------------------------------------------------------ caller


class OpenAICaller:
    def __init__(self, api_key: str, model: str, rpm: int, max_wall_s: float | None = None, *,
                 base_url: str = "https://api.openai.com/v1", timeout_s: float = 120.0,
                 max_output_tokens: int = 16000, reasoning_effort: str = "none",
                 transport: httpx.BaseTransport | None = None) -> None:
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
            transport=transport,
        )
        self._api_key = api_key
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = (reasoning_effort or "").strip()
        self.limiter = RateLimiter(rpm)
        self.usage = LLMUsage()
        self.deadline = time.monotonic() + max_wall_s if max_wall_s else None
        self.stage = "decide"

    def start_stage(self, name: str, max_wall_s: float | None) -> None:
        """Each stage (decide, chapters) gets its own wall-clock budget, so time
        spent rendering between them never eats into the next one."""
        self.stage = name
        self.deadline = time.monotonic() + max_wall_s if max_wall_s else None

    def generate(self, system_prompt: str, contents: str, schema: dict | None = None) -> OpenAIResponse:
        body = request_body(self.model, system_prompt, contents, schema,
                            max_output_tokens=self.max_output_tokens, reasoning_effort=self.reasoning_effort)
        rate_limited = server_errors = 0
        while True:
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise PipelineError(f"the AI {self.stage} stage ran past its time limit", code="timeout",
                                    retryable=True)
            self.usage.rate_limit_wait_s += self.limiter.acquire()
            try:
                http = _call_openai(self.client, body)
            except (httpx.LocalProtocolError, httpx.UnsupportedProtocol) as exc:
                # The request can't even be sent (a malformed header value, or
                # an OPENAI_BASE_URL without http(s)://): the same on every try.
                # h11's message quotes the offending header — the whole key —
                # so it is redacted and not chained (job.log prints the cause).
                raise DecisionError(
                    f"OpenAI request could not be sent, check OPENAI_API_KEY and OPENAI_BASE_URL "
                    f"({type(exc).__name__}: {_redact(str(exc), self._api_key)})", retryable=False) from None
            except httpx.TransportError as exc:
                # a blip worth retrying; its text gets the same treatment in
                # case a transport error ever quotes the request
                detail = f"{type(exc).__name__}: {_redact(str(exc), self._api_key)}"
                if server_errors < MAX_SERVER_ERROR_RETRIES:
                    server_errors += 1
                    self._backoff(server_errors, "network error", detail)
                    continue
                raise DecisionError(f"OpenAI network error: {detail}") from None
            status = http.status_code
            if status >= 400:
                kind, message = _error_info(http)
                message = _redact(message, self._api_key)
                if status == 429:
                    if kind == "insufficient_quota":
                        raise PipelineError(f"{QUOTA_MESSAGE}: {message}", code="llm_quota_exhausted",
                                            retryable=False)
                    rate_limited += 1
                    if rate_limited > MAX_RATE_LIMIT_RETRIES:
                        raise DecisionError(f"OpenAI kept rate-limiting after {rate_limited} waits: {message}")
                    asked = retry_wait(http, message)
                    wait = DEFAULT_429_WAIT_S if asked is None else asked
                    wait = min(MAX_429_WAIT_S, max(MIN_429_WAIT_S, wait))
                    logger.warning("openai rate limit (429), waiting %.1fs before retrying: %s", wait, message)
                    time.sleep(wait)
                    self.usage.rate_limit_wait_s += wait
                    self.usage.retries += 1
                    continue
                if status in RETRYABLE_HTTP_CODES or status >= 500:
                    if server_errors < MAX_SERVER_ERROR_RETRIES:
                        server_errors += 1
                        self._backoff(server_errors, f"server error {status}", message)
                        continue
                    raise DecisionError(f"OpenAI server error {status}: {message}")
                raise DecisionError(f"OpenAI API error {status}: {message}",
                                    retryable=status not in PERMANENT_HTTP_CODES)
            try:
                data = http.json()
            except ValueError as exc:
                # a proxy's HTML page or a cut-off body: treat like a server error
                if server_errors < MAX_SERVER_ERROR_RETRIES:
                    server_errors += 1
                    self._backoff(server_errors, "unreadable response", exc)
                    continue
                raise DecisionError(f"OpenAI returned an unreadable response: {exc}") from exc
            response = parse_response(data)
            self.usage.calls += 1
            self.usage.prompt_tokens += int(response.usage.get("input_tokens", 0) or 0)
            self.usage.output_tokens += int(response.usage.get("output_tokens", 0) or 0)
            return response

    def _backoff(self, attempt: int, what: str, detail: object) -> None:
        wait = 5.0 * attempt
        logger.warning("openai %s, retrying in %.0fs: %s", what, wait, detail)
        time.sleep(wait)
        self.usage.retries += 1
