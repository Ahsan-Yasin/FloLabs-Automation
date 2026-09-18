import json

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


def _call_gemini(
    client, model: str, segments: list[Segment], system_prompt: str, retry_note: str = ""
) -> str:
    from google.genai import types

    prompt = _segments_payload(segments)
    if retry_note:
        prompt = f"{retry_note}\n\n{prompt}"

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
        ),
    )
    return response.text


def _parse(raw: str) -> list[Decision]:
    data = json.loads(raw)
    return _decision_list_adapter.validate_python(data)


def get_decisions(segments: list[Segment], mode: str = "crosstalk") -> list[Decision]:
    """LLM edit-decision pass (section 3.3). Validates schema, retries once, fails loudly."""
    if not segments:
        return []
    if mode not in _SYSTEM_PROMPTS:
        raise ValueError(f"unknown decide mode {mode!r}, expected one of {sorted(_SYSTEM_PROMPTS)}")

    from google import genai

    settings = get_settings()
    if not settings.gemini_api_key:
        raise DecisionError("GEMINI_API_KEY is not set")

    from google.genai import errors as genai_errors

    client = genai.Client(api_key=settings.gemini_api_key)
    system_prompt = _SYSTEM_PROMPTS[mode]

    last_error: Exception | None = None
    for attempt, retry_note in enumerate(
        ["", "Your previous response was not a valid JSON array matching the required schema. Try again, strictly."]
    ):
        try:
            raw = _call_gemini(client, settings.gemini_model, segments, system_prompt, retry_note)
            decisions = _parse(raw)
        except (json.JSONDecodeError, ValidationError, KeyError) as exc:
            logger.warning("gemini decision parse failed on attempt %d: %s", attempt + 1, exc)
            last_error = exc
            continue
        except genai_errors.APIError as exc:
            # Transient overload/rate-limit (5xx/429) — worth one retry, not just malformed output.
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

    raise DecisionError(f"LLM returned invalid decisions after retry: {last_error}") from last_error
