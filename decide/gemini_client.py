"""Gemini calls for the v2 decide stage (plan D4, D17).

`GeminiCaller` is the one place that talks to the API: it enforces a
client-side requests-per-minute limit, backs off on 429 using the server's own
retryDelay, fails fast with `llm_quota_exhausted` when a DAILY quota is hit
(retrying can't help until it resets), retries transient 5xx, enforces the
decide stage's wall-clock limit and counts calls/tokens.

`judge_segments` is the per-chunk cleanup + scoring pass. It keeps the
hard-won robustness of the v1 client — a truncated (MAX_TOKENS) chunk is split
immediately, an invalid-but-complete one gets one same-size retry and is then
split, a single segment that still fails raises DecisionError — and adds:
index echo + exact index-set validation (so a split chunk can never be
mis-assigned), and incremental persistence to decisions.json so a crash or a
decide_only run never pays for the same chunk twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from core.config import get_settings
from core.errors import PipelineError
from core.logging import get_logger
from core.models import Decision, Moment, Segment, SegmentJudgment

from .prompts import (
    cleanup_scoring_prompt,
    judgment_schema,
    rerank_prompt,
    rerank_schema,
)

logger = get_logger(__name__)

RETRY_NOTE = (
    "Your previous response was not valid: it must be a JSON object matching the required schema with exactly "
    "one judgment per input segment, each echoing that segment's index. Try again, strictly."
)
DECISIONS_VERSION = 2
MAX_RATE_LIMIT_RETRIES = 6
MAX_SERVER_ERROR_RETRIES = 3
DEFAULT_429_WAIT_S = 20.0
MAX_429_WAIT_S = 90.0


class DecisionError(RuntimeError):
    """Raised when the LLM fails to return a valid decision list after retrying."""


# ------------------------------------------------------------------ transport


def _call_gemini(client, model: str, system_prompt: str, contents: str, schema: dict):
    """The single network call (tests replace this)."""
    from google.genai import types

    return client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_json_schema=schema,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )


def _finish_reason(response) -> str:
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

    def as_dict(self) -> dict[str, float]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
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


class GeminiCaller:
    def __init__(self, client, model: str, rpm: int, max_wall_s: float | None = None) -> None:
        self.client = client
        self.model = model
        self.limiter = RateLimiter(rpm)
        self.usage = LLMUsage()
        self.deadline = time.monotonic() + max_wall_s if max_wall_s else None

    def generate(self, system_prompt: str, contents: str, schema: dict):
        from google.genai import errors as genai_errors

        rate_limited = server_errors = 0
        while True:
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise PipelineError("the AI decision stage ran past decide_max_wall_s", code="timeout",
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
                            retry_after_s=int(delay or 3600),
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
                    wait = 5.0 * server_errors
                    logger.warning("gemini server error %s, retrying in %.0fs: %s", code, wait, exc)
                    time.sleep(wait)
                    self.usage.retries += 1
                    continue
                raise DecisionError(f"Gemini API error: {exc}") from exc
            self.usage.calls += 1
            meta = getattr(response, "usage_metadata", None)
            if meta is not None:
                self.usage.prompt_tokens += int(getattr(meta, "prompt_token_count", 0) or 0)
                self.usage.output_tokens += int(getattr(meta, "candidates_token_count", 0) or 0)
            return response


class LazyCaller:
    """Creates the real client on first use, so a job whose decisions are all
    saved (a decide_only job being rendered) makes no API call at all."""

    def __init__(self) -> None:
        self.model = get_settings().gemini_model
        self.usage = LLMUsage()
        self._real: GeminiCaller | None = None

    def generate(self, system_prompt: str, contents: str, schema: dict):
        if self._real is None:
            self._real = make_caller()
            self._real.usage = self.usage
        return self._real.generate(system_prompt, contents, schema)


def make_caller() -> GeminiCaller:
    from google import genai

    settings = get_settings()
    if not settings.gemini_api_key:
        raise DecisionError("GEMINI_API_KEY is not set")
    client = genai.Client(api_key=settings.gemini_api_key)
    return GeminiCaller(client, settings.gemini_model, settings.gemini_rpm, settings.decide_max_wall_s)


# ------------------------------------------------------------------ judging


def _segments_payload(chunk: list[tuple[int, Segment]]) -> str:
    return json.dumps(
        [
            {
                "index": i,
                "speaker": s.speaker,
                "start": round(s.start, 2),
                "end": round(s.end, 2),
                "text": s.text,
                "overlap_candidate": s.overlap_candidate,
            }
            for i, s in chunk
        ],
        ensure_ascii=False,
    )


def _parse_judgments(raw: str, chunk: list[tuple[int, Segment]]) -> list[SegmentJudgment]:
    data = json.loads(raw)
    items = data.get("judgments") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise TypeError("response has no judgments list")
    by_index: dict[int, dict] = {}
    for item in items:
        idx = item["index"]
        if idx in by_index:
            raise ValueError(f"index {idx} judged twice")
        by_index[idx] = item
    expected = [i for i, _ in chunk]
    if set(by_index) != set(expected):
        missing = sorted(set(expected) - set(by_index))[:5]
        extra = sorted(set(by_index) - set(expected))[:5]
        raise ValueError(f"judged indices do not match the input (missing {missing}, unexpected {extra})")
    out = []
    for i, seg in chunk:
        item = dict(by_index[i])
        item.update(index=i, start=seg.start, end=seg.end, speaker=seg.speaker, text=seg.text)
        if item.get("decision") == "keep":
            item["removal_category"] = "none"
        out.append(SegmentJudgment.model_validate(item))
    return out


def _judge_chunk(caller: GeminiCaller, system_prompt: str, chunk: list[tuple[int, Segment]]) -> list[SegmentJudgment]:
    """Validate, retry once, split on truncation or on a second failure; a
    single segment that still fails raises DecisionError."""
    last_error: Exception | None = None
    for attempt, note in enumerate(["", RETRY_NOTE]):
        response = None
        payload = _segments_payload(chunk)
        try:
            response = caller.generate(system_prompt, f"{note}\n\n{payload}" if note else payload, judgment_schema())
            return _parse_judgments(response.text, chunk)
        except (json.JSONDecodeError, ValidationError, KeyError, TypeError, ValueError) as exc:
            reason = _finish_reason(response) if response is not None else "no response"
            if "MAX_TOKENS" in reason:
                logger.warning(
                    "gemini response for a %d-segment chunk was truncated (finish_reason=MAX_TOKENS) on attempt %d "
                    "— lower gemini_max_segments_per_call if this recurs: %s", len(chunk), attempt + 1, exc,
                )
                if len(chunk) > 1:
                    return _split(caller, system_prompt, chunk)
            else:
                logger.warning("gemini judgment parse failed on attempt %d (finish_reason=%s): %s",
                               attempt + 1, reason, exc)
            last_error = exc
    if len(chunk) > 1:
        logger.warning("gemini failed twice on a %d-segment chunk (%s) — splitting", len(chunk), last_error)
        return _split(caller, system_prompt, chunk)
    raise DecisionError(f"LLM returned invalid judgments after retry: {last_error}") from last_error


def _split(caller, system_prompt, chunk):
    mid = len(chunk) // 2
    logger.warning("splitting a %d-segment chunk into %d + %d and retrying", len(chunk), mid, len(chunk) - mid)
    return _judge_chunk(caller, system_prompt, chunk[:mid]) + _judge_chunk(caller, system_prompt, chunk[mid:])


def _fingerprint(model: str, system_prompt: str, segments: list[Segment]) -> str:
    h = hashlib.sha256()
    h.update(f"v{DECISIONS_VERSION}|{model}|".encode())
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
    caller: GeminiCaller | None = None,
    highlights_criteria: str | None = None,
    on_progress: callable[[int, int], None] | None = None,
    persist_path: Path | None = None,
) -> list[SegmentJudgment]:
    """One multi-label judgment per segment (keep/remove + highlight score),
    in chunks of `gemini_max_segments_per_call`, saved after every chunk."""
    if not segments:
        return []
    settings = get_settings()
    caller = caller or make_caller()
    system_prompt = cleanup_scoring_prompt(highlights_criteria or settings.highlights_criteria)
    fingerprint = _fingerprint(caller.model, system_prompt, segments)
    judged = _load_saved(persist_path, fingerprint)
    if judged:
        logger.info("reusing %d saved judgments from %s", len(judged), persist_path)

    indexed = list(enumerate(segments))
    size = max(1, settings.gemini_max_segments_per_call)
    total = len(segments)
    for start in range(0, total, size):
        chunk = indexed[start : start + size]
        todo = [(i, s) for i, s in chunk if i not in judged]
        if todo:
            for j in _judge_chunk(caller, system_prompt, todo):
                judged[j.index] = j
            _save(persist_path, fingerprint, caller.model, judged, total)
        if on_progress:
            on_progress(min(start + size, total), total)
    return [judged[i] for i in range(total)]


def get_decisions(
    segments: list[Segment],
    on_progress: callable[[int, int], None] | None = None,
    caller: GeminiCaller | None = None,
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


@dataclass
class RerankResult:
    moments: list[Moment]
    ok: bool
    note: str = ""
    raw: list = field(default_factory=list)


def rerank_moments(
    candidates: list[Moment],
    judgments: list[SegmentJudgment],
    *,
    caller: GeminiCaller | None = None,
    highlights_criteria: str | None = None,
    shorts_criteria: str | None = None,
    context_segments: int | None = None,
) -> RerankResult:
    """One global call over the best candidates: calibrated scores, titles,
    hooks, short-worthiness and a complete window. Never fatal: on failure the
    per-segment scores are used as they are."""
    if not candidates:
        return RerankResult([], True)
    settings = get_settings()
    caller = caller or make_caller()
    ctx = settings.rerank_context_segments if context_segments is None else context_segments
    last = len(judgments) - 1
    payload = []
    for m in candidates:
        lo, hi = max(0, m.first_index - ctx), min(last, m.last_index + ctx)
        payload.append({
            "id": m.id,
            "first_index": m.first_index,
            "last_index": m.last_index,
            "category": m.category,
            "segments": [
                {"index": j.index, "start": round(j.start, 1), "speaker": j.speaker, "text": j.text,
                 "context": not (m.first_index <= j.index <= m.last_index)}
                for j in judgments[lo : hi + 1]
            ],
        })
    system_prompt = rerank_prompt(highlights_criteria or settings.highlights_criteria,
                                  shorts_criteria or settings.shorts_criteria)
    contents = json.dumps(payload, ensure_ascii=False)
    by_id = {m.id: m for m in candidates}
    last_error = ""
    for attempt, note in enumerate(["", "Return one entry per candidate id, strictly matching the schema."]):
        try:
            response = caller.generate(system_prompt, f"{note}\n\n{contents}" if note else contents, rerank_schema())
            data = json.loads(response.text)
            items = data.get("moments") if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise TypeError("no moments list")
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError, DecisionError) as exc:
            last_error = str(exc)
            logger.warning("re-rank failed on attempt %d: %s", attempt + 1, exc)
            continue
        updated: dict[int, Moment] = {}
        for item in items:
            try:
                m = by_id[int(item["id"])]
                first, last_i = int(item["first_index"]), int(item["last_index"])
                score = max(0, min(10, int(item["score"])))
            except (KeyError, TypeError, ValueError):
                continue
            lo, hi = max(0, m.first_index - ctx), min(last, m.last_index + ctx)
            first, last_i = max(lo, min(first, hi)), max(lo, min(last_i, hi))
            if first > last_i or last_i < m.first_index or first > m.last_index:
                first, last_i = m.first_index, m.last_index  # window must overlap the scored core
            category = item.get("category") if item.get("category") in _HIGHLIGHT_OK else m.category
            updated[m.id] = m.model_copy(update={
                "first_index": first,
                "last_index": last_i,
                "start": judgments[first].start,
                "end": judgments[last_i].end,
                "score": float(score),
                "category": category,
                "title": _clean_title(item.get("title", ""), 60),
                "hook": _clean_title(item.get("hook", ""), 140),
                "short_worthy": bool(item.get("short_worthy")),
                "reranked": True,
            })
        moments = [updated.get(m.id, m) for m in candidates]
        missing = len(candidates) - len(updated)
        note_text = f"{missing} candidate(s) missing from the re-rank answer" if missing else ""
        return RerankResult(moments, True, note_text, items)
    return RerankResult(list(candidates), False, f"re-rank failed, using per-segment scores ({last_error})")


_HIGHLIGHT_OK = {"funny", "new_architecture", "new_feature", "concept", "decision", "insight"}
