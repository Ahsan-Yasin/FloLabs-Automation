"""Anthropic (Claude) calls for the decide stage.

`AnthropicCaller` has the same interface as `GeminiCaller` and `OpenAICaller`
(model, usage, limiter, deadline, stage, start_stage, generate), so
judge_segments, rerank_moments and generate_chapters work unchanged with any
provider.

It uses the official `anthropic` SDK (Messages API) with the SDK's own retries
turned off: this caller owns retrying, so the stage deadline, the usage counts
and the no-credit fail-fast behave the same for every provider. An account
without credit (402 billing_error, or the 400 "credit balance is too low")
fails fast with `llm_quota_exhausted`; a 429 waits for the time the server
asks for; 408/409/5xx/529 (overloaded) and network errors are retried with
backoff; a bad key / model / request fails the job as not retryable. Error
text is redacted: it reaches job.json, job.log and the UI.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import anthropic

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
from .openai_client import MIN_429_WAIT_S, _redact

logger = get_logger(__name__)

# Errors that retrying the same request can never fix (bad request, bad key,
# no access to the model, unknown model, request too large, invalid params).
PERMANENT_HTTP_CODES = {400, 401, 403, 404, 413, 422}
# Request timeout / conflict: safe to retry, like 5xx and 529 (overloaded).
RETRYABLE_HTTP_CODES = {408, 409}
QUOTA_MESSAGE = (
    "the Anthropic account has no credit left — add credits at console.anthropic.com "
    "(Settings > Billing), then retry the job"
)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _model_params(model: str) -> dict:
    """Per-model request settings.

    Claude Sonnet 5.5 thinks by default (reasoning is billed as output) and
    rejects thinking "disabled"; "between_tools" is its thinking-off switch —
    the one-line-per-sentence answers need no reasoning. It also gets the
    server-side refusal fallback: if a safety classifier declines a chunk, the
    API re-runs it on a fallback model in the same call instead of failing.
    Haiku 4.5 doesn't think unless asked, so it needs neither."""
    if model.startswith("claude-sonnet-5-5"):
        return {"thinking": {"type": "between_tools"}, "betas": [FALLBACK_BETA], "fallbacks": "default"}
    return {}


@dataclass
class ClaudeResponse:
    """What the decide code reads from an answer (the same .text the other
    providers' responses have, plus finish_reason in Gemini's MAX_TOKENS
    vocabulary)."""

    text: str
    finish_reason: str
    usage: dict = field(default_factory=dict)


# ------------------------------------------------------------------ transport


def request_params(model: str, system_prompt: str, contents: str, schema: dict | None = None, *,
                   max_output_tokens: int) -> dict:
    params: dict = {
        "model": model,
        "max_tokens": max_output_tokens,
        # The system prompt is the same on every call of a job, so it is
        # marked for caching; a prefix shorter than the model's minimum
        # (4096 tokens on Haiku 4.5, 1024 on most larger models) is simply
        # not cached, at no cost.
        "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": contents}],
        **_model_params(model),
    }
    if schema is not None:
        params["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    return params


def _call_anthropic(client: anthropic.Anthropic, params: dict):
    """The single network call (tests replace this or the HTTP transport).
    Beta features (Sonnet 5.5's thinking-off switch and fallback) live on
    the beta endpoint."""
    if params.get("betas"):
        return client.beta.messages.create(**params)
    return client.messages.create(**params)


def parse_message(message) -> ClaudeResponse:
    """Join the text blocks of the answer. A refusal yields empty text, which
    the callers already treat as an invalid answer (retry, then split)."""
    stop = str(getattr(message, "stop_reason", None) or "unknown")
    texts = [block.text for block in (message.content or []) if getattr(block, "type", "") == "text"]
    for block in message.content or []:
        if getattr(block, "type", "") == "fallback":
            logger.warning("claude: the requested model declined a chunk; %s answered it instead",
                           getattr(message, "model", "the fallback model"))
    if stop == "refusal":
        details = getattr(message, "stop_details", None)
        logger.warning("claude refused to answer (%s)", getattr(details, "category", None))
        texts = []
    finish = "MAX_TOKENS" if stop == "max_tokens" else stop
    usage = message.usage.model_dump() if getattr(message, "usage", None) is not None else {}
    return ClaudeResponse("".join(texts), finish, usage)


# ------------------------------------------------------------------ errors


def _is_no_credit(exc: anthropic.APIStatusError) -> bool:
    """402 billing_error, or the 400 Anthropic sends when the prepaid balance
    has run out ("Your credit balance is too low to access the Anthropic API")."""
    if exc.status_code == 402 or exc.type == "billing_error":
        return True
    return exc.status_code == 400 and "credit balance" in str(exc.message or "").lower()


def retry_wait(exc: anthropic.APIStatusError) -> float | None:
    """How long the server asked us to wait before retrying a 429, if it said."""
    headers = exc.response.headers
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            return float(value) * scale
        except ValueError:
            pass  # an HTTP date: fall back to the default wait
    return None


# ------------------------------------------------------------------ caller


class AnthropicCaller:
    def __init__(self, api_key: str, model: str, rpm: int, max_wall_s: float | None = None, *,
                 timeout_s: float = 120.0, max_output_tokens: int = 16000,
                 http_client: anthropic.DefaultHttpxClient | None = None) -> None:
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=0,
                                          http_client=http_client)
        self._api_key = api_key
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.limiter = RateLimiter(rpm)
        self.usage = LLMUsage()
        self.deadline = time.monotonic() + max_wall_s if max_wall_s else None
        self.stage = "decide"

    def start_stage(self, name: str, max_wall_s: float | None) -> None:
        """Each stage (decide, chapters) gets its own wall-clock budget, so time
        spent rendering between them never eats into the next one."""
        self.stage = name
        self.deadline = time.monotonic() + max_wall_s if max_wall_s else None

    def generate(self, system_prompt: str, contents: str, schema: dict | None = None) -> ClaudeResponse:
        params = request_params(self.model, system_prompt, contents, schema, max_output_tokens=self.max_output_tokens)
        rate_limited = server_errors = 0
        while True:
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise PipelineError(f"the AI {self.stage} stage ran past its time limit", code="timeout",
                                    retryable=True)
            self.usage.rate_limit_wait_s += self.limiter.acquire()
            try:
                message = _call_anthropic(self.client, params)
            except anthropic.APIStatusError as exc:
                status = exc.status_code
                detail = _redact(str(exc.message or exc), self._api_key)
                if _is_no_credit(exc):
                    raise PipelineError(f"{QUOTA_MESSAGE}: {detail}", code="llm_quota_exhausted",
                                        retryable=False) from None
                if status == 429:
                    rate_limited += 1
                    if rate_limited > MAX_RATE_LIMIT_RETRIES:
                        raise DecisionError(f"Claude kept rate-limiting after {rate_limited} waits: {detail}") from None
                    asked = retry_wait(exc)
                    wait = DEFAULT_429_WAIT_S if asked is None else asked
                    wait = min(MAX_429_WAIT_S, max(MIN_429_WAIT_S, wait))
                    logger.warning("claude rate limit (429), waiting %.1fs before retrying: %s", wait, detail)
                    time.sleep(wait)
                    self.usage.rate_limit_wait_s += wait
                    self.usage.retries += 1
                    continue
                if status in RETRYABLE_HTTP_CODES or status >= 500:
                    if server_errors < MAX_SERVER_ERROR_RETRIES:
                        server_errors += 1
                        what = "overloaded (529)" if status == 529 else f"server error {status}"
                        self._backoff(server_errors, what, detail)
                        continue
                    raise DecisionError(f"Claude server error {status}: {detail}") from None
                raise DecisionError(f"Claude API error {status}: {detail}",
                                    retryable=status not in PERMANENT_HTTP_CODES) from None
            except anthropic.APIConnectionError as exc:
                # network failure or timeout before a response: a blip worth retrying
                detail = f"{type(exc).__name__}: {_redact(str(exc), self._api_key)}"
                if server_errors < MAX_SERVER_ERROR_RETRIES:
                    server_errors += 1
                    self._backoff(server_errors, "network error", detail)
                    continue
                raise DecisionError(f"Claude network error: {detail}") from None
            response = parse_message(message)
            self.usage.calls += 1
            # cached and cache-written input tokens are input too
            self.usage.prompt_tokens += sum(int(response.usage.get(k) or 0) for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            # cache reads cost a tenth of normal input, cache writes 1.25x: kept
            # apart so the report's cost estimate is right
            self.usage.cache_read_tokens += int(response.usage.get("cache_read_input_tokens") or 0)
            self.usage.cache_write_tokens += int(response.usage.get("cache_creation_input_tokens") or 0)
            self.usage.output_tokens += int(response.usage.get("output_tokens") or 0)
            return response

    def _backoff(self, attempt: int, what: str, detail: object) -> None:
        wait = 5.0 * attempt
        logger.warning("claude %s, retrying in %.0fs: %s", what, wait, detail)
        time.sleep(wait)
        self.usage.retries += 1
