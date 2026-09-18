import json
import logging

import pytest

from core.config import get_settings
from core.models import Segment
from decide import gemini_client
from decide.gemini_client import DecisionError, get_decisions


class _FakeCandidate:
    def __init__(self, finish_reason=None):
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, text, finish_reason=None):
        self.text = text
        self.candidates = [_FakeCandidate(finish_reason)] if finish_reason else []


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _segments(n=2):
    base = [
        Segment(speaker="A", start=0.0, end=2.0, text="the main point is x"),
        Segment(speaker="B", start=2.0, end=2.5, text="yeah yeah", overlap_candidate=True),
    ]
    out = []
    for i in range(n):
        s = base[i % 2]
        out.append(Segment(speaker=s.speaker, start=float(i), end=float(i) + 1, text=s.text))
    return out


def _decisions_json(segments, decision="keep"):
    return json.dumps(
        [{"start": s.start, "end": s.end, "decision": decision, "reason": "r"} for s in segments]
    )


def test_returns_decisions_on_valid_first_response(monkeypatch):
    segments = _segments()
    valid = _decisions_json(segments)
    monkeypatch.setattr(gemini_client, "_call_gemini", lambda *a, **k: _FakeResponse(valid))

    decisions = get_decisions(segments)
    assert len(decisions) == 2


def test_retries_once_on_malformed_json_then_succeeds(monkeypatch):
    segments = _segments()
    calls = {"n": 0}
    valid = _decisions_json(segments)

    def fake_call(client, model, segs, system_prompt, retry_note=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse("not json")
        return _FakeResponse(valid)

    monkeypatch.setattr(gemini_client, "_call_gemini", fake_call)
    decisions = get_decisions(segments)
    assert calls["n"] == 2
    assert len(decisions) == 2


def test_fails_loudly_after_second_malformed_response(monkeypatch):
    monkeypatch.setattr(gemini_client, "_call_gemini", lambda *a, **k: _FakeResponse("still not json"))
    with pytest.raises(DecisionError):
        get_decisions(_segments())


def test_truncated_response_logs_max_tokens_diagnostic(monkeypatch, caplog):
    """A response cut off mid-JSON (the real-world bug: long meetings producing
    enough segments that a single response exceeds the model's max output
    tokens) should be distinguishable in the logs from a generically malformed
    response, via the API's finish_reason."""
    monkeypatch.setattr(
        gemini_client,
        "_call_gemini",
        lambda *a, **k: _FakeResponse('[{"start": 0.0, "end": 2.0, "decision": "keep", "reason": "unterminat',
        finish_reason="MAX_TOKENS"),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(DecisionError):
        get_decisions(_segments())
    assert any("MAX_TOKENS" in record.message for record in caplog.records)


def test_empty_segments_short_circuits(monkeypatch):
    monkeypatch.setattr(
        gemini_client, "_call_gemini", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called"))
    )
    assert get_decisions([]) == []


def test_highlights_mode_uses_highlights_prompt(monkeypatch):
    segments = _segments()
    captured = {}
    valid = _decisions_json(segments)

    def fake_call(client, model, segs, system_prompt, retry_note=""):
        captured["system_prompt"] = system_prompt
        return _FakeResponse(valid)

    monkeypatch.setattr(gemini_client, "_call_gemini", fake_call)
    get_decisions(segments, mode="highlights")
    assert captured["system_prompt"] == gemini_client.HIGHLIGHTS_SYSTEM_PROMPT


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        get_decisions(_segments(), mode="bogus")


def test_large_segment_list_is_split_into_multiple_calls(monkeypatch):
    """The bug this guards against: a long meeting produces enough segments
    that describing all of them (plus their decisions) in one response
    overflows the model's max output tokens, truncating the JSON mid-string.
    Segments must be batched so no single call ever has to return more than
    `gemini_max_segments_per_call` decisions."""
    monkeypatch.setenv("GEMINI_MAX_SEGMENTS_PER_CALL", "10")
    get_settings.cache_clear()

    segments = _segments(25)
    call_sizes = []

    def fake_call(client, model, segs, system_prompt, retry_note=""):
        call_sizes.append(len(segs))
        return _FakeResponse(_decisions_json(segs))

    monkeypatch.setattr(gemini_client, "_call_gemini", fake_call)
    decisions = get_decisions(segments)

    assert call_sizes == [10, 10, 5]
    assert len(decisions) == 25
    assert [d.start for d in decisions] == [s.start for s in segments]
    get_settings.cache_clear()


def test_default_chunk_size_keeps_typical_meeting_in_one_call(monkeypatch):
    segments = _segments(30)
    call_sizes = []

    def fake_call(client, model, segs, system_prompt, retry_note=""):
        call_sizes.append(len(segs))
        return _FakeResponse(_decisions_json(segs))

    monkeypatch.setattr(gemini_client, "_call_gemini", fake_call)
    get_decisions(segments)
    assert call_sizes == [30]


def test_max_tokens_truncation_splits_chunk_instead_of_blind_retry(monkeypatch):
    """Regression test for the real-world failure: `gemini_max_segments_per_call`
    only bounds segment *count*, so a chunk whose segments happen to carry a lot
    of text can still truncate (MAX_TOKENS) even at the configured chunk size.
    Blindly retrying the identical chunk just truncates again and raises
    DecisionError ("...Unterminated string..."). The fix must instead split the
    truncated chunk and retry the halves, so a chunk that would have failed at
    size N still succeeds once it's small enough.
    """
    segments = _segments(8)
    call_sizes = []

    def fake_call(client, model, segs, system_prompt, retry_note=""):
        call_sizes.append(len(segs))
        if len(segs) > 2:
            # Oversized requests truncate mid-JSON regardless of retry_note.
            return _FakeResponse('[{"start": 0.0, "end": 1.0,', finish_reason="MAX_TOKENS")
        return _FakeResponse(_decisions_json(segs))

    monkeypatch.setattr(gemini_client, "_call_gemini", fake_call)
    decisions = gemini_client.get_decisions(segments)

    assert len(decisions) == 8
    assert [d.start for d in decisions] == [s.start for s in segments]
    # The oversized chunk (size 8) is only ever requested once — a blind retry
    # would have called it again at the same size and failed again. Instead it
    # gets split down until calls succeed at size <=2.
    assert call_sizes.count(8) == 1
    assert call_sizes.count(2) >= 1


def test_missing_field_on_complete_response_splits_after_retry_fails(monkeypatch):
    """Regression test for the real-world failure report: a COMPLETE, non-
    truncated JSON array (finish_reason=STOP, not MAX_TOKENS) where a handful
    of entries are missing the required `decision` field — pydantic raises
    ValidationError, e.g. "12 validation errors for list[Decision] ... Field
    required". Unlike MAX_TOKENS this isn't a hard ceiling, so it still gets
    the normal same-size retry first; only once that ALSO fails should the
    chunk split and retry the halves, rather than giving up on the whole batch.
    """
    segments = _segments(8)
    call_sizes = []
    attempts_at_size_8 = {"n": 0}

    def fake_call(client, model, segs, system_prompt, retry_note=""):
        call_sizes.append(len(segs))
        if len(segs) == 8:
            attempts_at_size_8["n"] += 1
            # A complete response (not truncated) with one entry missing "decision".
            bad = [{"start": s.start, "end": s.end, "decision": "keep", "reason": "r"} for s in segs]
            del bad[1]["decision"]
            return _FakeResponse(json.dumps(bad), finish_reason="STOP")
        return _FakeResponse(_decisions_json(segs))

    monkeypatch.setattr(gemini_client, "_call_gemini", fake_call)
    decisions = gemini_client.get_decisions(segments)

    assert len(decisions) == 8
    assert [d.start for d in decisions] == [s.start for s in segments]
    # Both attempts at the original size were used (unlike MAX_TOKENS, a
    # same-size retry is worth trying here) before falling back to splitting.
    assert attempts_at_size_8["n"] == 2
    assert call_sizes.count(4) >= 1


def test_rate_limit_429_backs_off_before_retry(monkeypatch):
    """A 429 (free-tier RPM/TPM quota exceeded) must not be retried immediately —
    resending into the same rate-limit window just fails again. This asserts the
    retry actually pauses (time.sleep) rather than firing back-to-back."""
    from google.genai import errors as genai_errors

    sleeps = []
    monkeypatch.setattr(gemini_client.time, "sleep", lambda s: sleeps.append(s))

    segments = _segments()
    calls = {"n": 0}
    valid = _decisions_json(segments)

    def fake_call(client, model, segs, system_prompt, retry_note=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise genai_errors.ClientError(429, {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}})
        return _FakeResponse(valid)

    monkeypatch.setattr(gemini_client, "_call_gemini", fake_call)
    decisions = get_decisions(segments)

    assert len(decisions) == 2
    assert sleeps and sleeps[0] > 0


def test_one_chunk_failing_does_not_affect_others(monkeypatch):
    """Each chunk retries/fails independently — one bad chunk shouldn't have
    to spoil decisions for the rest of a long meeting, and a failure should
    still surface loudly rather than silently dropping segments."""
    monkeypatch.setenv("GEMINI_MAX_SEGMENTS_PER_CALL", "5")
    get_settings.cache_clear()

    segments = _segments(10)

    def fake_call(client, model, segs, system_prompt, retry_note=""):
        if segs[0].start == 0.0:
            return _FakeResponse("not json", finish_reason="MAX_TOKENS")
        return _FakeResponse(_decisions_json(segs))

    monkeypatch.setattr(gemini_client, "_call_gemini", fake_call)
    with pytest.raises(DecisionError):
        get_decisions(segments)
    get_settings.cache_clear()
