"""LLM calls for the v2 decide stage (plan D4, D17): the provider-neutral
judging / re-rank code, plus the Gemini transport.

`make_caller` picks the provider from `llm_provider`: OpenAI (default, see
openai_client.py) or Gemini. Both callers share one interface (model, usage,
start_stage, generate) and the same rules: a client-side requests-per-minute
limit and per-request HTTP timeout, waiting on 429 for the time the server
asks, failing fast with `llm_quota_exhausted` when retrying can't help (a
Gemini DAILY quota, an OpenAI account without credit), retrying transient 5xx
and network errors, the current stage's wall-clock limit, and call/token
counts.

`judge_segments` is the per-chunk cleanup + scoring pass. It keeps the
hard-won robustness of the v1 client — a truncated (MAX_TOKENS) chunk is split
immediately, an invalid-but-complete one gets one same-size retry and is then
split, a single segment that still fails raises DecisionError — and adds:
index echo + exact index-set validation (so a split chunk can never be
mis-assigned), read-only context lines at chunk edges, one re-score of a chunk
whose scores collapsed to zero, and incremental persistence to decisions.json
so a crash or a decide_only run never pays for the same chunk twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

from pydantic import ValidationError

from core.config import Settings, get_settings
from core.errors import PipelineError
from core.logging import get_logger
from core.models import Decision, Moment, Segment, SegmentJudgment

from .prompts import (
    HIGHLIGHT_CODES,
    REMOVAL_CODES,
    RESCORE_NOTE,
    judge_prompt,
    rerank_prompt,
    transcript_lines,
)

if TYPE_CHECKING:
    from .anthropic_client import AnthropicCaller
    from .openai_client import OpenAICaller

logger = get_logger(__name__)

RETRY_NOTE = (
    "Your previous answer was not valid: answer with exactly one line per JUDGE line, in order, in the form "
    '"<n> <k|r> <score> <removal code or -> <highlight code or ->", and nothing else. Try again, strictly.'
)
DECISIONS_VERSION = 3
MAX_RATE_LIMIT_RETRIES = 6
MAX_SERVER_ERROR_RETRIES = 3
DEFAULT_429_WAIT_S = 20.0
MAX_429_WAIT_S = 90.0
# 4xx that retrying the same request can never fix (bad key, bad model name,
# bad request, no permission).
PERMANENT_HTTP_CODES = {400, 401, 403, 404}


class DecisionError(RuntimeError):
    """The LLM step failed. `retryable` says whether re-running the job can help
    (a flaky answer or network) or not (a bad API key or model name)."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


# ------------------------------------------------------------------ transport


def _call_gemini(client, model: str, system_prompt: str, contents: str, schema: dict | None = None):
    """The single network call (tests replace this). With a JSON schema the
    answer is constrained JSON; without one it is plain text (what the v2
    prompts use: pretty-printed JSON cost ~4x the output tokens)."""
    from google.genai import types

    if schema is not None:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_json_schema=schema,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
    else:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="text/plain",
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
    return client.models.generate_content(model=model, contents=contents, config=config)


def _finish_reason(response) -> str:
    """Why the model stopped. OpenAI responses carry it directly (already
    mapped to "MAX_TOKENS" on truncation); Gemini's is on the candidate."""
    reason = getattr(response, "finish_reason", None)
    if reason is not None:
        return str(reason)
    candidates = getattr(response, "candidates", None) or []
    if candidates and getattr(candidates[0], "finish_reason", None) is not None:
        return str(candidates[0].finish_reason)
    return "unknown"


@dataclass
class LLMUsage:
    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    rate_limit_wait_s: float = 0.0
    retries: int = 0
    # parts of prompt_tokens served from / written to the prompt cache (Claude)
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def as_dict(self) -> dict[str, float]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "rate_limit_wait_s": round(self.rate_limit_wait_s, 1),
            "retries": self.retries,
        }


class RateLimiter:
    """At most `rpm` requests in any 60-second window."""

    def __init__(self, rpm: int) -> None:
        self.rpm = max(1, rpm)
        self._stamps: deque[float] = deque()

    def acquire(self) -> float:
        now = time.monotonic()
        while self._stamps and now - self._stamps[0] >= 60.0:
            self._stamps.popleft()
        if len(self._stamps) < self.rpm:
            self._stamps.append(now)
            return 0.0
        wait = self._stamps[0] + 60.0 - now
        time.sleep(wait)
        # Take the slot the oldest request frees, whether or not the clock moved.
        self._stamps.popleft()
        self._stamps.append(now + wait)
        return wait


def _parse_duration(value) -> float | None:
    if value is None:
        return None
    m = re.match(r"^\s*([\d.]+)\s*s?\s*$", str(value))
    return float(m.group(1)) if m else None


def _quota_info(exc) -> tuple[bool, float | None]:
    """(is_daily_quota, retry_delay_s) from a 429's error details."""
    daily, delay = False, None
    details = getattr(exc, "details", None)
    err = details.get("error", details) if isinstance(details, dict) else {}
    for item in (err or {}).get("details") or []:
        kind = str(item.get("@type", ""))
        if kind.endswith("RetryInfo"):
            delay = _parse_duration(item.get("retryDelay"))
        if kind.endswith("QuotaFailure"):
            for violation in item.get("violations") or []:
                quota = f"{violation.get('quotaId', '')} {violation.get('quotaMetric', '')}".lower()
                if "perday" in quota or "per_day" in quota:
                    daily = True
    message = str(getattr(exc, "message", "") or "").lower()
    if "per day" in message or "perday" in message:
        daily = True
    return daily, delay


def seconds_until_quota_reset(now: datetime | None = None) -> int:
    """Gemini's per-day quotas reset at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Los_Angeles")
    except Exception:  # noqa: BLE001 — no tz database: fall back to an hour
        return 3600
    now = (now or datetime.now(tz)).astimezone(tz)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((midnight - now).total_seconds()))


def _network_errors() -> tuple[type[BaseException], ...]:
    try:
        import httpx

        return (httpx.TransportError,)
    except ImportError:  # pragma: no cover
        return ()


class GeminiCaller:
    def __init__(self, client, model: str, rpm: int, max_wall_s: float | None = None) -> None:
        self.client = client
        self.model = model
        self.limiter = RateLimiter(rpm)
        self.usage = LLMUsage()
        self.deadline = time.monotonic() + max_wall_s if max_wall_s else None
        self.stage = "decide"

    def start_stage(self, name: str, max_wall_s: float | None) -> None:
        """Each stage (decide, chapters) gets its own wall-clock budget, so time
        spent rendering between them never eats into the next one."""
        self.stage = name
        self.deadline = time.monotonic() + max_wall_s if max_wall_s else None

    def generate(self, system_prompt: str, contents: str, schema: dict | None = None):
        from google.genai import errors as genai_errors

        network = _network_errors()
        rate_limited = server_errors = 0
        while True:
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise PipelineError(f"the AI {self.stage} stage ran past its time limit", code="timeout",
                                    retryable=True)
            self.usage.rate_limit_wait_s += self.limiter.acquire()
            try:
                response = _call_gemini(self.client, self.model, system_prompt, contents, schema)
            except genai_errors.APIError as exc:
                code = getattr(exc, "code", None)
                if code == 429:
                    daily, delay = _quota_info(exc)
                    if daily:
                        raise PipelineError(
                            f"Gemini daily quota exhausted: {exc}", code="llm_quota_exhausted", retryable=True,
                            retry_after_s=seconds_until_quota_reset(),
                        ) from exc
                    rate_limited += 1
                    if rate_limited > MAX_RATE_LIMIT_RETRIES:
                        raise DecisionError(f"Gemini kept rate-limiting after {rate_limited} waits: {exc}") from exc
                    wait = min(MAX_429_WAIT_S, delay if delay is not None else DEFAULT_429_WAIT_S)
                    logger.warning("gemini rate limit (429), waiting %.0fs before retrying: %s", wait, exc)
                    time.sleep(wait)
                    self.usage.rate_limit_wait_s += wait
                    self.usage.retries += 1
                    continue
                if code is not None and 500 <= int(code) < 600 and server_errors < MAX_SERVER_ERROR_RETRIES:
                    server_errors += 1
                    self._backoff(server_errors, f"server error {code}", exc)
                    continue
                permanent = code is not None and int(code) in PERMANENT_HTTP_CODES
                raise DecisionError(f"Gemini API error: {exc}", retryable=not permanent) from exc
            except network as exc:
                if server_errors < MAX_SERVER_ERROR_RETRIES:
                    server_errors += 1
                    self._backoff(server_errors, "network error", exc)
                    continue
                raise DecisionError(f"Gemini network error: {exc}") from exc
            self.usage.calls += 1
            meta = getattr(response, "usage_metadata", None)
            if meta is not None:
                self.usage.prompt_tokens += int(getattr(meta, "prompt_token_count", 0) or 0)
                self.usage.output_tokens += int(getattr(meta, "candidates_token_count", 0) or 0)
            return response

    def _backoff(self, attempt: int, what: str, exc: BaseException) -> None:
        wait = 5.0 * attempt
        logger.warning("gemini %s, retrying in %.0fs: %s", what, wait, exc)
        time.sleep(wait)
        self.usage.retries += 1


class LazyCaller:
    """Creates the real client on first use, so a job whose decisions are all
    saved (a decide_only job being rendered) makes no API call at all."""

    def __init__(self) -> None:
        settings = get_settings()
        # part of the decisions.json / rerank.json fingerprints: switching
        # provider or model re-judges instead of reusing another model's answers
        self.model = llm_model(settings)
        self.usage = LLMUsage()
        self._real: GeminiCaller | OpenAICaller | AnthropicCaller | None = None
        self._stage: tuple[str, float | None] = ("decide", settings.decide_max_wall_s)

    def start_stage(self, name: str, max_wall_s: float | None) -> None:
        self._stage = (name, max_wall_s)
        if self._real is not None:
            self._real.start_stage(name, max_wall_s)

    def generate(self, system_prompt: str, contents: str, schema: dict | None = None):
        if self._real is None:
            self._real = make_caller()
            self._real.usage = self.usage
            self._real.start_stage(*self._stage)
        return self._real.generate(system_prompt, contents, schema)


# Anything judge_segments / rerank_moments / generate_chapters accept.
Caller: TypeAlias = "GeminiCaller | OpenAICaller | AnthropicCaller | LazyCaller"


def _provider(settings: Settings) -> str:
    return (settings.llm_provider or "").strip().lower()


def llm_model(settings: Settings) -> str:
    """The model the configured provider will use ("" for an unknown
    provider, which make_caller reports)."""
    return {
        "anthropic": settings.anthropic_model,
        "openai": settings.openai_model,
        "gemini": settings.gemini_model,
    }.get(_provider(settings), "")


def _api_key(value: str, name: str) -> str:
    """The key as it can go into an HTTP header. .env keeps the spaces inside
    a quoted value (and an OS env var keeps a trailing newline); httpx then
    rejects the header with an error that quotes it — the full key in job.error
    and job.log — or, for a curly quote, fails deep inside the judging loop as
    a retryable "invalid answer". Either way: fail once, clearly, keyless."""
    key = (value or "").strip()
    if not key:
        raise DecisionError(f"{name} is not set", retryable=False)
    if not (key.isascii() and key.isprintable()) or any(c.isspace() for c in key):
        raise DecisionError(f"{name} contains invalid characters (check for quotes or spaces pasted with it)",
                            retryable=False)
    return key


def make_caller() -> GeminiCaller | OpenAICaller | AnthropicCaller:
    settings = get_settings()
    provider = _provider(settings)
    if provider == "anthropic":
        from .anthropic_client import AnthropicCaller

        key = _api_key(settings.anthropic_api_key, "ANTHROPIC_API_KEY")
        return AnthropicCaller(
            key, settings.anthropic_model, settings.anthropic_rpm, settings.decide_max_wall_s,
            timeout_s=settings.anthropic_request_timeout_s, max_output_tokens=settings.anthropic_max_output_tokens,
        )
    if provider == "openai":
        from .openai_client import OpenAICaller

        key = _api_key(settings.openai_api_key, "OPENAI_API_KEY")
        return OpenAICaller(
            key, settings.openai_model, settings.openai_rpm, settings.decide_max_wall_s,
            base_url=settings.openai_base_url, timeout_s=settings.openai_request_timeout_s,
            max_output_tokens=settings.openai_max_output_tokens, reasoning_effort=settings.openai_reasoning_effort,
        )
    if provider != "gemini":
        raise DecisionError(f"unknown LLM_PROVIDER {settings.llm_provider!r} (use anthropic, openai or gemini)",
                            retryable=False)

    from google import genai
    from google.genai import types

    # the Gemini key goes out in a header too (x-goog-api-key)
    key = _api_key(settings.gemini_api_key, "GEMINI_API_KEY")
    client = genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=int(settings.gemini_request_timeout_s * 1000)),
    )
    return GeminiCaller(client, settings.gemini_model, settings.gemini_rpm, settings.decide_max_wall_s)


# ------------------------------------------------------------------ judging


def _judge_contents(chunk: list[tuple[int, Segment]], before: list[tuple[int, Segment]],
                    after: list[tuple[int, Segment]], note: str = "") -> str:
    parts = [note] if note else []
    if before:
        parts += ["CONTEXT (do not judge):", *transcript_lines(before, prefix="~")]
    parts += ["JUDGE:", *transcript_lines(chunk)]
    if after:
        parts += ["CONTEXT (do not judge):", *transcript_lines(after, prefix="~")]
    return "\n".join(parts)


_JUDGE_LINE = re.compile(r"^~?\[?(\d+)\]?[.:]?\s+([kr])\s+(\d{1,2})((?:\s+\S+){0,3})$", re.IGNORECASE)


def _answer_lines(raw: str) -> list[str]:
    """Non-empty lines of a plain-text answer, without markdown code fences."""
    return [ln.strip() for ln in (raw or "").splitlines() if ln.strip() and not ln.strip().startswith("```")]


# "no code" placeholders. The format line in the prompt reads "<removal code
# or -> <highlight code or ->", and Claude Haiku copies the "->" (seen on a
# real meeting: every retry and every split repeated it until the job failed).
_NO_CODE = frozenset({"-", "->", "→", "–", "—"})


def parse_judge_lines(raw: str) -> list[dict]:
    """'312 r 0 fill -' lines -> dicts. The model sometimes drops one of the
    '-' placeholders ('313 k 5 arch'); the codes are told apart by value
    (removal and highlight codes never overlap). Any other deviation makes the
    whole answer invalid (-> retry / split)."""
    items = []
    for line in _answer_lines(raw):
        m = _JUDGE_LINE.match(line)
        if not m:
            raise ValueError(f"unparsable judgment line {line[:60]!r}")
        n, d, score, codes = m.groups()
        removal = highlight = "-"
        for token in codes.split():
            token = token.lower()
            if token in REMOVAL_CODES:
                removal = token
            elif token in HIGHLIGHT_CODES:
                highlight = token
            elif token.isdigit():
                continue  # the model occasionally repeats a number ("126 k 4 4 idea"); the first one is the score
            elif token in _NO_CODE:
                continue
            else:
                raise ValueError(f"unknown code {token!r} in {line[:60]!r}")
        items.append({"i": int(n), "d": d.lower(), "s": int(score), "c": removal, "h": highlight})
    if not items:
        raise ValueError("empty answer")
    return items


def _parse_judgments(raw: str, chunk: list[tuple[int, Segment]]) -> list[SegmentJudgment]:
    items = parse_judge_lines(raw)
    expected = {i for i, _ in chunk}
    by_index: dict[int, dict] = {}
    for item in items:
        idx = item["i"]
        if idx not in expected:
            # the model sometimes also answers for the read-only context
            # lines; those answers are ignored (rejecting them cost a retry
            # and a cascade of splits on the real meeting)
            continue
        if idx in by_index:
            raise ValueError(f"line {idx} judged twice")
        by_index[idx] = item
    if set(by_index) != expected:
        missing = sorted(expected - set(by_index))[:5]
        raise ValueError(f"judged lines do not match the input (missing {missing})")
    out = []
    for i, seg in chunk:
        item = by_index[i]
        decision = {"k": "keep", "r": "remove"}.get(item["d"])
        if decision is None:
            raise ValueError(f"line {i}: bad decision {item['d']!r}")
        score = int(item["s"])
        if not 0 <= score <= 10:
            raise ValueError(f"line {i}: score {score} out of range")
        category = HIGHLIGHT_CODES.get(item.get("h", ""), "none")
        # categories mean "worth a look" from 3 up — jokes included: the owner
        # wants highlights and shorts people can learn from, with funny moments
        # only when they are really good (no head start for light moments)
        if score < 3:
            category = "none"
        removal = REMOVAL_CODES.get(item.get("c", ""), "none") if decision == "remove" else "none"
        out.append(SegmentJudgment(
            index=i, start=seg.start, end=seg.end, speaker=seg.speaker, text=seg.text,
            decision=decision, reason="", removal_category=removal, highlight_score=score,
            highlight_category=category,
        ))
    return out


def _context(chunk, indexed, context):
    first, last = chunk[0][0], chunk[-1][0]
    return indexed[max(0, first - context) : first], indexed[last + 1 : last + 1 + context]


def _judge_chunk(caller, system_prompt: str, chunk: list[tuple[int, Segment]], indexed: list[tuple[int, Segment]],
                 context: int) -> list[SegmentJudgment]:
    """Validate, retry once, split on truncation or on a second failure; a
    single segment that still fails raises DecisionError."""
    before, after = _context(chunk, indexed, context)
    last_error: Exception | None = None
    for attempt, note in enumerate(["", RETRY_NOTE]):
        response = None
        try:
            response = caller.generate(system_prompt, _judge_contents(chunk, before, after, note))
            return _parse_judgments(response.text, chunk)
        except (json.JSONDecodeError, ValidationError, KeyError, TypeError, ValueError) as exc:
            reason = _finish_reason(response) if response is not None else "no response"
            if "MAX_TOKENS" in reason:
                logger.warning(
                    "LLM response for a %d-segment chunk was truncated (finish_reason=MAX_TOKENS) on attempt %d "
                    "— lower gemini_max_segments_per_call if this recurs: %s", len(chunk), attempt + 1, exc,
                )
                if len(chunk) > 1:
                    return _split(caller, system_prompt, chunk, indexed, context)
            else:
                logger.warning("LLM judgment parse failed on attempt %d (finish_reason=%s): %s",
                               attempt + 1, reason, exc)
            last_error = exc
    if len(chunk) > 1:
        logger.warning("LLM failed twice on a %d-segment chunk (%s) — splitting", len(chunk), last_error)
        return _split(caller, system_prompt, chunk, indexed, context)
    raise DecisionError(f"LLM returned invalid judgments after retry: {last_error}") from last_error


def _split(caller, system_prompt, chunk, indexed, context):
    mid = len(chunk) // 2
    logger.warning("splitting a %d-segment chunk into %d + %d and retrying", len(chunk), mid, len(chunk) - mid)
    return (_judge_chunk(caller, system_prompt, chunk[:mid], indexed, context)
            + _judge_chunk(caller, system_prompt, chunk[mid:], indexed, context))


def scores_collapsed(judgments: list[SegmentJudgment], min_kept: int = 12) -> bool:
    """True when a chunk kept plenty of content but scored all of it 0 — seen
    on real meetings for whole chunks of substantive talk."""
    kept = [j for j in judgments if j.decision == "keep"]
    return len(kept) >= min_kept and all(j.highlight_score == 0 for j in kept)


def _rescore(caller, system_prompt, chunk, indexed, context, first_try):
    """One more attempt with a note about the anchors; keep the original
    answer if the retry is no better or fails."""
    before, after = _context(chunk, indexed, context)
    try:
        response = caller.generate(system_prompt, _judge_contents(chunk, before, after, RESCORE_NOTE))
        second = _parse_judgments(response.text, chunk)
    except (json.JSONDecodeError, ValidationError, KeyError, TypeError, ValueError, DecisionError) as exc:
        logger.warning("re-score of a collapsed chunk failed, keeping the first answer: %s", exc)
        return first_try
    return first_try if scores_collapsed(second) else second


def _fingerprint(model: str, system_prompt: str, segments: list[Segment], chunk_size: int, context: int) -> str:
    h = hashlib.sha256()
    h.update(f"v{DECISIONS_VERSION}|{model}|{chunk_size}|{context}|".encode())
    h.update(system_prompt.encode())
    for s in segments:
        h.update(f"|{s.start:.3f}|{s.end:.3f}|{s.speaker}|{s.text}".encode())
    return h.hexdigest()


def _load_saved(path: Path | None, fingerprint: str) -> dict[int, SegmentJudgment]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if data.get("version") != DECISIONS_VERSION or data.get("fingerprint") != fingerprint:
        return {}
    try:
        return {j["index"]: SegmentJudgment.model_validate(j) for j in data.get("judgments", [])}
    except ValidationError:
        return {}


def _save(path: Path | None, fingerprint: str, model: str, judged: dict[int, SegmentJudgment], total: int) -> None:
    if path is None:
        return
    data = {
        "version": DECISIONS_VERSION,
        "fingerprint": fingerprint,
        "model": model,
        "total_segments": total,
        "judgments": [judged[i].model_dump() for i in sorted(judged)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def judge_segments(
    segments: list[Segment],
    *,
    caller: Caller | None = None,
    highlights_criteria: str | None = None,
    on_progress: callable[[int, int], None] | None = None,
    persist_path: Path | None = None,
) -> list[SegmentJudgment]:
    """One judgment per segment (keep/remove + highlight score), in chunks of
    `gemini_max_segments_per_call`, saved after every chunk."""
    if not segments:
        return []
    settings = get_settings()
    caller = caller or make_caller()
    system_prompt = judge_prompt(highlights_criteria or settings.highlights_criteria)
    size = max(1, settings.gemini_max_segments_per_call)
    context = max(0, settings.gemini_context_segments)
    fingerprint = _fingerprint(caller.model, system_prompt, segments, size, context)
    judged = _load_saved(persist_path, fingerprint)
    if judged:
        logger.info("reusing %d saved judgments from %s", len(judged), persist_path)

    indexed = list(enumerate(segments))
    total = len(segments)
    rescores_left = settings.gemini_max_rescores
    for start in range(0, total, size):
        chunk = indexed[start : start + size]
        todo = [(i, s) for i, s in chunk if i not in judged]
        if todo:
            results = _judge_chunk(caller, system_prompt, todo, indexed, context)
            if rescores_left > 0 and scores_collapsed(results):
                rescores_left -= 1
                logger.info("chunk at segment %d scored every kept line 0 — re-scoring once", todo[0][0])
                results = _rescore(caller, system_prompt, todo, indexed, context, results)
            for j in results:
                judged[j.index] = j
            _save(persist_path, fingerprint, caller.model, judged, total)
        if on_progress:
            on_progress(min(start + size, total), total)
    return [judged[i] for i in range(total)]


def get_decisions(
    segments: list[Segment],
    on_progress: callable[[int, int], None] | None = None,
    caller: Caller | None = None,
) -> list[Decision]:
    """keep/remove decisions only (compatibility wrapper around judge_segments)."""
    return [j.to_decision() for j in judge_segments(segments, caller=caller, on_progress=on_progress)]


# ------------------------------------------------------------------ re-rank


def _clean_title(text: str, limit: int) -> str:
    text = re.sub(r"[<>\r\n\t\"]+", " ", str(text or "")).strip()
    text = re.sub(r"\s{2,}", " ", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


_CONNECTIVE = re.compile(
    r"^(?:and|but|so|or|which|because|then|that|also|plus|um|uh|like|the (?:first|second|third|next|other) one)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")


def complete_window(first: int, last: int, judgments: list[SegmentJudgment], limit_lo: int, limit_hi: int,
                    max_steps: int = 2) -> tuple[int, int]:
    """Don't start a clip mid-thought or end it mid-sentence: step back over
    lines that start with a connective or a lowercase letter, and forward
    while the last line has no sentence end (at most `max_steps` each way,
    within [limit_lo, limit_hi])."""
    steps = 0
    while steps < max_steps and first > limit_lo:
        text = judgments[first].text.strip()
        if not text or not (text[0].islower() or _CONNECTIVE.match(text)):
            break
        first -= 1
        steps += 1
    steps = 0
    while steps < max_steps and last < limit_hi and not _SENTENCE_END.search(judgments[last].text.strip()):
        last += 1
        steps += 1
    return first, last


@dataclass
class RerankResult:
    moments: list[Moment]
    ok: bool
    note: str = ""
    raw: list = field(default_factory=list)


def _rerank_contents(candidates: list[Moment], judgments: list[SegmentJudgment], ctx: int) -> str:
    last = len(judgments) - 1
    blocks = []
    for m in candidates:
        lo, hi = max(0, m.first_index - ctx), min(last, m.last_index + ctx)
        lines = [f"#{m.id} core {m.first_index}-{m.last_index}"]
        prev_speaker = None
        for j in judgments[lo : hi + 1]:
            mark = "" if m.first_index <= j.index <= m.last_index else "~"
            who = f"{j.speaker}: " if j.speaker and j.speaker != prev_speaker else ""
            prev_speaker = j.speaker or prev_speaker
            lines.append(f"{mark}[{j.index}] {who}{j.text}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


_RERANK_LINE = re.compile(r"^#?(\d+)\s*\|\s*(\d{1,2})\s*\|\s*(\w+)\s*\|\s*~?\[?(\d+)\]?\s*\|\s*~?\[?(\d+)\]?"
                          r"\s*\|\s*([yn])\w*\s*\|([^|]*)\|?(.*)$", re.IGNORECASE)


def parse_rerank_lines(raw: str) -> list[dict]:
    """'<id>|<score>|<code>|<a>|<b>|<y/n>|<title>|<why>' lines. Lines that
    don't fit are skipped (the candidate then counts as unanswered)."""
    items = []
    for line in _answer_lines(raw):
        m = _RERANK_LINE.match(line)
        if m:
            cid, score, code, a, b, short, title, hook = m.groups()
            # the model sometimes closes the line with a stray "|" ("...for sharing.|")
            items.append({"id": int(cid), "s": int(score), "h": code.lower(), "a": int(a), "b": int(b),
                          "sh": short.lower() == "y", "t": title.strip(" |\t"), "k": hook.strip(" |\t")})
    return items


def _apply_rerank(m: Moment, item: dict, judgments: list[SegmentJudgment], ctx: int) -> Moment:
    last = len(judgments) - 1
    first, last_i = int(item["a"]), int(item["b"])
    score = max(0, min(10, int(item["s"])))
    lo, hi = max(0, m.first_index - ctx), min(last, m.last_index + ctx)
    first, last_i = max(lo, min(first, hi)), max(lo, min(last_i, hi))
    if first > last_i or last_i < m.first_index or first > m.last_index:
        first, last_i = m.first_index, m.last_index  # window must overlap the scored core
    first, last_i = complete_window(first, last_i, judgments, max(0, lo - 2), min(last, hi + 2))
    category = HIGHLIGHT_CODES.get(item.get("h", ""), m.category)
    peak = m.peak_index if m.peak_index is not None and first <= m.peak_index <= last_i else None
    return m.model_copy(update={
        "first_index": first,
        "last_index": last_i,
        "start": judgments[first].start,
        "end": judgments[last_i].end,
        "score": float(score),
        "category": category,
        "peak_index": peak,
        "title": _clean_title(item.get("t", ""), 60),
        "hook": _clean_title(item.get("k", ""), 140),
        "short_worthy": bool(item.get("sh")),
        "reranked": True,
    })


def rerank_moments(
    candidates: list[Moment],
    judgments: list[SegmentJudgment],
    *,
    caller: Caller | None = None,
    highlights_criteria: str | None = None,
    shorts_criteria: str | None = None,
    context_segments: int | None = None,
) -> RerankResult:
    """One global call over the best candidates: calibrated scores, titles,
    hooks, short-worthiness and a complete window; one follow-up call only
    for candidates the answer skipped. Never fatal for bad answers or API
    errors: unanswered candidates keep their per-segment scores."""
    if not candidates:
        return RerankResult([], True)
    settings = get_settings()
    caller = caller or make_caller()
    ctx = settings.rerank_context_segments if context_segments is None else context_segments
    system_prompt = rerank_prompt(highlights_criteria or settings.highlights_criteria,
                                  shorts_criteria or settings.shorts_criteria)
    by_id = {m.id: m for m in candidates}
    updated: dict[int, Moment] = {}
    raw_items: list = []
    last_error = ""
    pending = list(candidates)
    for attempt in range(2):
        note = "" if attempt == 0 else (
            "Answer for these candidates too — exactly one line per id, in the same format.")
        contents = _rerank_contents(pending, judgments, ctx)
        try:
            response = caller.generate(system_prompt, f"{note}\n\n{contents}" if note else contents)
            items = parse_rerank_lines(response.text)
        except (ValueError, TypeError, AttributeError, DecisionError) as exc:
            last_error = str(exc)
            logger.warning("re-rank failed on attempt %d: %s", attempt + 1, exc)
            continue
        for item in items:
            m = by_id.get(item["id"])
            if m is None or m.id in updated:
                continue
            updated[m.id] = _apply_rerank(m, item, judgments, ctx)
            raw_items.append(item)
        pending = [m for m in candidates if m.id not in updated]
        if not pending:
            break
        logger.warning("re-rank answered %d of %d candidates", len(updated), len(candidates))
    if not updated:
        return RerankResult(list(candidates), False, f"re-rank failed, using per-segment scores ({last_error})")
    moments = [updated.get(m.id, m) for m in candidates]
    missing = len(candidates) - len(updated)
    note_text = f"{missing} candidate(s) missing from the re-rank answer" if missing else ""
    return RerankResult(moments, True, note_text, raw_items)
