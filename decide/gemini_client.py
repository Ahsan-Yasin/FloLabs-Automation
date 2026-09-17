import json

from pydantic import TypeAdapter, ValidationError

from core.config import get_settings
from core.logging import get_logger
from core.models import Decision, Segment

logger = get_logger(__name__)

_decision_list_adapter = TypeAdapter(list[Decision])

SYSTEM_PROMPT = """You are editing a meeting recording transcript to remove crosstalk \
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


def _call_gemini(client, model: str, segments: list[Segment], retry_note: str = "") -> str:
    from google.genai import types

    prompt = _segments_payload(segments)
    if retry_note:
        prompt = f"{retry_note}\n\n{prompt}"

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
        ),
    )
    return response.text


def _parse(raw: str) -> list[Decision]:
    data = json.loads(raw)
    return _decision_list_adapter.validate_python(data)


def get_decisions(segments: list[Segment]) -> list[Decision]:
    """LLM edit-decision pass (section 3.3). Validates schema, retries once, fails loudly."""
    if not segments:
        return []

    from google import genai

    settings = get_settings()
    if not settings.gemini_api_key:
        raise DecisionError("GEMINI_API_KEY is not set")

    client = genai.Client(api_key=settings.gemini_api_key)

    last_error: Exception | None = None
    for attempt, retry_note in enumerate(
        ["", "Your previous response was not a valid JSON array matching the required schema. Try again, strictly."]
    ):
        try:
            raw = _call_gemini(client, settings.gemini_model, segments, retry_note)
            decisions = _parse(raw)
        except (json.JSONDecodeError, ValidationError, KeyError) as exc:
            logger.warning("gemini decision parse failed on attempt %d: %s", attempt + 1, exc)
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
