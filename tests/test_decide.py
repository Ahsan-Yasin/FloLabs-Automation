import json

import pytest

from core.config import get_settings
from core.models import Segment
from decide import gemini_client
from decide.gemini_client import DecisionError, get_decisions


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _segments():
    return [
        Segment(speaker="A", start=0.0, end=2.0, text="the main point is x"),
        Segment(speaker="B", start=2.0, end=2.5, text="yeah yeah", overlap_candidate=True),
    ]


def test_returns_decisions_on_valid_first_response(monkeypatch):
    valid = json.dumps(
        [
            {"start": 0.0, "end": 2.0, "decision": "keep", "reason": "content"},
            {"start": 2.0, "end": 2.5, "decision": "remove", "reason": "crosstalk"},
        ]
    )
    monkeypatch.setattr(gemini_client, "_call_gemini", lambda *a, **k: valid)

    decisions = get_decisions(_segments())
    assert [d.decision for d in decisions] == ["keep", "remove"]


def test_retries_once_on_malformed_json_then_succeeds(monkeypatch):
    calls = {"n": 0}
    valid = json.dumps(
        [
            {"start": 0.0, "end": 2.0, "decision": "keep", "reason": "content"},
            {"start": 2.0, "end": 2.5, "decision": "remove", "reason": "crosstalk"},
        ]
    )

    def fake_call(client, model, segments, retry_note=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json"
        return valid

    monkeypatch.setattr(gemini_client, "_call_gemini", fake_call)
    decisions = get_decisions(_segments())
    assert calls["n"] == 2
    assert len(decisions) == 2


def test_fails_loudly_after_second_malformed_response(monkeypatch):
    monkeypatch.setattr(gemini_client, "_call_gemini", lambda *a, **k: "still not json")
    with pytest.raises(DecisionError):
        get_decisions(_segments())


def test_empty_segments_short_circuits(monkeypatch):
    monkeypatch.setattr(
        gemini_client, "_call_gemini", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called"))
    )
    assert get_decisions([]) == []
