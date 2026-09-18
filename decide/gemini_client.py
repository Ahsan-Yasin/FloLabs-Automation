import json
import time

from pydantic import TypeAdapter, ValidationError

from core.config import get_settings
from core.logging import get_logger
from core.models import Decision, Segment

logger = get_logger(__name__)

_decision_list_adapter = TypeAdapter(list[Decision])

CROSSTALK_SYSTEM_PROMPT = """You are editing a meeting recording transcript to remove crosstalk \
and low-value overlapping speech, while keeping every segment that carries real content.

You will be given a JSON list of speaker-turn segments in chronological order. Each has:
- speaker: a diarization label (may be wrong — do not use it to judge importance)
- start, end: seconds
- text: what was said
- overlap_candidate: true if ASR-level word timing suggests this segment overlaps in \
time with an adjacent segment from a different speaker

For EVERY segment, decide "keep" or "remove":
- "remove" ONLY for crosstalk/interruptions/simultaneous speech that carries no \
distinguishable content on its own (filler like "yeah", "mhm", "sorry go ahead", \
false starts talked over, someone trying to interject without landing a point).
- "keep" for anything that makes a point, answers a question, asks a question, or \
would be missed by a listener — even if overlap_candidate is true. Overlap alone is \
NOT a reason to remove; only remove when the overlapping speech is actually low-value.
- When in doubt, keep it. Under-removing is much cheaper than cutting real content.
also remove  talk unreated to the meeting like my weekedn was fun and unrelated generla talk like that\
the context of the meeting is important and if the segment is not related to the meeting then remove it.\

Return a JSON array with exactly one object per input segment, in the same order, \
each with fields: start (number), end (number), decision ("keep" or "remove"), \
reason (short string). Copy start/end exactly from the input segment you're deciding on."""

HIGHLIGHTS_SYSTEM_PROMPT = """You are cutting a raw video down to a short highlight reel — \
only the best, most interesting, funniest, most insightful, or most important moments.

You will be given a JSON list of chronological speaker-turn segments. Each has:
- speaker: a diarization label (may be wrong — ignore it)
- start, end: seconds
- text: what was said
- overlap_candidate: true if this segment's audio overlaps an adjacent one

For EVERY segment, decide "keep" or "remove":
- "keep" ONLY for genuine highlight-worthy moments: a strong point, a punchline, a \
surprising or emotional beat, a key result or answer, something a viewer would clip \
and share on its own.
- "remove" for setup, filler, rambling, small talk, repeated points, low-energy \
moments, and anything that isn't independently compelling — even if it's on-topic and \
coherent. Most segments should be "remove"; this is an aggressive highlight cut, not a \
light trim.
- A segment does not qualify just because it makes sense in context — it must stand on \
its own as something worth watching out of order, in a short reel.

Return a JSON array with exactly one object per input segment, in the same order, \
each with fields: start (number), end (number), decision ("keep" or "remove"), \
reason (short string). Copy start/end exactly from the input segment you're deciding on."""

_SYSTEM_PROMPTS = {
    "crosstalk": CROSSTALK_SYSTEM_PROMPT,
    "highlights": HIGHLIGHTS_SYSTEM_PROMPT,
}


class DecisionError(RuntimeError):
    """Raised when the LLM fails to return a valid decision list after retrying."""


def _segments_payload(segments: list[Segment]) -> str:
    return json.dumps(
        [
            {
                "speaker": s.speaker,
                "start": s.start,
                "end": s.end,
                "text": s.text,
                "overlap_candidate": s.overlap_candidate,
            }
            for s in segments
        ]
    )


def _call_gemini(client, model: str, segments: list[Segment], system_prompt: str, retry_note: str = ""):
    from google.genai import types

    prompt = _segments_payload(segments)
    if retry_note:
        prompt = f"{retry_note}\n\n{prompt}"

    return client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
        ),
    )


def _parse(raw: str) -> list[Decision]:
    data = json.loads(raw)
    return _decision_list_adapter.validate_python(data)


def _finish_reason(response) -> str:
    candidates = getattr(response, "candidates", None) or []
    if candidates and candidates[0].finish_reason is not None:
        return str(candidates[0].finish_reason)
    return "unknown"


def _chunk(segments: list[Segment], size: int) -> list[list[Segment]]:
    size = max(1, size)
    return [segments[i : i + size] for i in range(0, len(segments), size)]


def _split_and_retry(client, model: str, segments: list[Segment], system_prompt: str) -> list[Decision]:
    mid = len(segments) // 2
    logger.warning("splitting a %d-segment chunk into %d + %d and retrying", len(segments), mid, len(segments) - mid)
    return _decisions_for_chunk(client, model, segments[:mid], system_prompt) + _decisions_for_chunk(
        client, model, segments[mid:], system_prompt
    )


def _decisions_for_chunk(client, model: str, segments: list[Segment], system_prompt: str) -> list[Decision]:
    """One chunk's worth of the LLM edit-decision pass (section 3.3). Validates
    schema, retries once, then falls back to splitting the chunk, fails loudly
    only once a single segment can't be resolved.

    A fixed `gemini_max_segments_per_call` only bounds segment *count* — it does
    nothing for a chunk whose segments happen to carry a lot of text (long
    monologue turns, verbose reasons), which can still blow past the model's
    max output tokens and truncate mid-JSON (finish_reason=MAX_TOKENS). Retrying
    that exact same chunk is pointless there: same input size in, same
    truncation out, so a MAX_TOKENS truncation splits immediately rather than
    spending the retry on an unchanged request.

    Separately — not a truncation at all — the model can also return a
    complete, valid JSON array that's individually wrong: one or more entries
    missing a required field (seen in practice: `decision` silently dropped on
    a handful of entries scattered through a long response), or the wrong
    number of entries. Unlike truncation this genuinely can succeed on a
    same-size retry (it's model drift on a long generation, not a hard
    ceiling), so it still gets the normal retry first — but if that also
    fails, the fallback is the same: shrink the batch and retry the halves
    independently, since compliance reliably improves as batch size drops.
    This converges to single segments, where a persistent failure still
    surfaces via DecisionError instead of silently dropping content.
    """
    from google.genai import errors as genai_errors

    last_error: Exception | None = None
    attempt = 0
    for attempt, retry_note in enumerate(
        ["", "Your previous response was not a valid JSON array matching the required schema. Try again, strictly."]
    ):
        response = None
        try:
            response = _call_gemini(client, model, segments, system_prompt, retry_note)
            decisions = _parse(response.text)
        except (json.JSONDecodeError, ValidationError, KeyError) as exc:
            reason = _finish_reason(response) if response is not None else "no response"
            if "MAX_TOKENS" in reason:
                logger.warning(
                    "gemini response for a %d-segment chunk was truncated (finish_reason=MAX_TOKENS) on "
                    "attempt %d — lower gemini_max_segments_per_call if this recurs: %s",
                    len(segments), attempt + 1, exc,
                )
                if len(segments) > 1:
                    return _split_and_retry(client, model, segments, system_prompt)
            else:
                logger.warning(
                    "gemini decision parse failed on attempt %d (finish_reason=%s): %s",
                    attempt + 1, reason, exc,
                )
            last_error = exc
            continue
        except genai_errors.APIError as exc:
            # Transient overload/rate-limit (5xx/429) — worth one retry, not just malformed output.
            # A 429 (free-tier RPM/TPM quota) needs an actual pause before the retry: firing the
            # retry immediately just resends into the same rate-limit window and fails again.
            if getattr(exc, "code", None) == 429:
                logger.warning(
                    "gemini rate limit (429) hit on attempt %d for a %d-segment chunk — "
                    "backing off before retrying: %s",
                    attempt + 1, len(segments), exc,
                )
                time.sleep(20)
            else:
                logger.warning("gemini API error on attempt %d: %s", attempt + 1, exc)
            last_error = exc
            continue

        if len(decisions) != len(segments):
            logger.warning(
                "gemini returned %d decisions for %d segments on attempt %d",
                len(decisions), len(segments), attempt + 1,
            )
            last_error = DecisionError("decision count did not match segment count")
            continue

        return decisions

    if len(segments) > 1:
        logger.warning(
            "gemini failed on a %d-segment chunk after %d attempts (%s) — splitting and retrying",
            len(segments), attempt + 1, last_error,
        )
        return _split_and_retry(client, model, segments, system_prompt)

    raise DecisionError(f"LLM returned invalid decisions after retry: {last_error}") from last_error


def get_decisions(segments: list[Segment], mode: str = "crosstalk") -> list[Decision]:
    """LLM edit-decision pass (section 3.3), batched so long meetings with many
    segments can't produce a single response large enough to hit the model's
    max output tokens and get cut off mid-JSON."""
    if not segments:
        return []
    if mode not in _SYSTEM_PROMPTS:
        raise ValueError(f"unknown decide mode {mode!r}, expected one of {sorted(_SYSTEM_PROMPTS)}")

    from google import genai

    settings = get_settings()
    if not settings.gemini_api_key:
        raise DecisionError("GEMINI_API_KEY is not set")

    client = genai.Client(api_key=settings.gemini_api_key)
    system_prompt = _SYSTEM_PROMPTS[mode]

    decisions: list[Decision] = []
    for chunk in _chunk(segments, settings.gemini_max_segments_per_call):
        decisions.extend(_decisions_for_chunk(client, settings.gemini_model, chunk, system_prompt))
    return decisions
