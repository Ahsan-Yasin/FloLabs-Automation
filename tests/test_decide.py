import json
import logging

import pytest
from google.genai import errors as genai_errors

from core.config import get_settings
from core.errors import PipelineError
from core.models import Moment, Segment
from decide import gemini_client
from decide.gemini_client import (
    DecisionError,
    GeminiCaller,
    RateLimiter,
    get_decisions,
    judge_segments,
    rerank_moments,
)


class _Meta:
    prompt_token_count = 100
    candidates_token_count = 50


class _FakeCandidate:
    def __init__(self, finish_reason=None):
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, text, finish_reason=None):
        self.text = text
        self.candidates = [_FakeCandidate(finish_reason)] if finish_reason else []
        self.usage_metadata = _Meta()


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()
    sleeps = []
    monkeypatch.setattr(gemini_client.time, "sleep", lambda s: sleeps.append(s))
    yield sleeps
    get_settings.cache_clear()


def _caller(rpm=1000, max_wall_s=None):
    return GeminiCaller(client=None, model="test-model", rpm=rpm, max_wall_s=max_wall_s)


def _segments(n=2):
    texts = ["the main point is x", "yeah yeah"]
    return [Segment(speaker="AB"[i % 2], start=float(i), end=float(i) + 1, text=f"{texts[i % 2]} {i}") for i in range(n)]


def _payload(contents: str) -> list[dict]:
    return json.loads(contents[contents.index("["):])


def _answer(contents, decision="keep", **overrides):
    items = []
    for seg in _payload(contents):
        item = {"index": seg["index"], "decision": decision, "reason": "r", "removal_category": "none",
                "highlight_score": 2, "highlight_category": "none"}
        item.update(overrides)
        items.append(item)
    return json.dumps({"judgments": items})


def _fake(monkeypatch, fn):
    monkeypatch.setattr(gemini_client, "_call_gemini", fn)


# ---------------------------------------------------------------- judging


def test_valid_first_response_fills_segment_fields(monkeypatch):
    _fake(monkeypatch, lambda c, m, sp, contents, schema: _FakeResponse(_answer(contents, decision="remove",
                                                                                  removal_category="filler")))
    judgments = judge_segments(_segments(3), caller=_caller())
    assert [j.index for j in judgments] == [0, 1, 2]
    assert judgments[1].text == "yeah yeah 1" and judgments[1].start == 1.0 and judgments[1].speaker == "B"
    assert all(j.decision == "remove" and j.removal_category == "filler" for j in judgments)


def test_keep_forces_removal_category_none(monkeypatch):
    _fake(monkeypatch, lambda c, m, sp, contents, schema: _FakeResponse(_answer(contents, removal_category="filler")))
    assert all(j.removal_category == "none" for j in judge_segments(_segments(), caller=_caller()))


def test_schema_and_criteria_are_sent(monkeypatch):
    seen = {}

    def fake(client, model, system_prompt, contents, schema):
        seen.update(system=system_prompt, schema=schema, model=model)
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, fake)
    judge_segments(_segments(), caller=_caller(), highlights_criteria="dragons and spaceships")
    assert "dragons and spaceships" in seen["system"]
    assert seen["schema"]["properties"]["judgments"]["items"]["required"][0] == "index"
    assert seen["model"] == "test-model"


def test_retries_once_on_malformed_json_then_succeeds(monkeypatch):
    calls = []

    def fake(client, model, system_prompt, contents, schema):
        calls.append(contents)
        return _FakeResponse("not json" if len(calls) == 1 else _answer(contents))

    _fake(monkeypatch, fake)
    assert len(judge_segments(_segments(), caller=_caller())) == 2
    assert len(calls) == 2 and calls[1].startswith("Your previous response was not valid")


def test_fails_loudly_when_a_single_segment_never_parses(monkeypatch):
    _fake(monkeypatch, lambda *a: _FakeResponse("still not json"))
    with pytest.raises(DecisionError):
        judge_segments(_segments(1), caller=_caller())


def test_truncation_splits_instead_of_blind_retry(monkeypatch, caplog):
    sizes = []

    def fake(client, model, system_prompt, contents, schema):
        n = len(_payload(contents))
        sizes.append(n)
        if n > 2:
            return _FakeResponse('{"judgments": [{"index": 0,', finish_reason="MAX_TOKENS")
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, fake)
    with caplog.at_level(logging.WARNING):
        judgments = judge_segments(_segments(8), caller=_caller())
    assert [j.index for j in judgments] == list(range(8))
    assert sizes.count(8) == 1 and sizes.count(2) >= 1
    assert any("MAX_TOKENS" in r.message for r in caplog.records)


def test_missing_field_gets_same_size_retry_then_split(monkeypatch):
    attempts_at_8 = []

    def fake(client, model, system_prompt, contents, schema):
        if len(_payload(contents)) == 8:
            attempts_at_8.append(1)
            data = json.loads(_answer(contents))
            del data["judgments"][1]["decision"]
            return _FakeResponse(json.dumps(data), finish_reason="STOP")
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, fake)
    assert len(judge_segments(_segments(8), caller=_caller())) == 8
    assert len(attempts_at_8) == 2


@pytest.mark.parametrize("mutate", ["wrong_index", "duplicate", "missing"])
def test_index_set_must_match_exactly(monkeypatch, mutate):
    """A response that judges the wrong segments must never be accepted —
    that is what could silently mis-assign keep/remove after a split."""
    bad_first = {"n": 0}

    def fake(client, model, system_prompt, contents, schema):
        data = json.loads(_answer(contents))
        if bad_first["n"] < 2 and len(data["judgments"]) > 1:
            bad_first["n"] += 1
            if mutate == "wrong_index":
                data["judgments"][0]["index"] = 999
            elif mutate == "duplicate":
                data["judgments"][1]["index"] = data["judgments"][0]["index"]
            else:
                data["judgments"].pop()
        return _FakeResponse(json.dumps(data))

    _fake(monkeypatch, fake)
    judgments = judge_segments(_segments(4), caller=_caller())
    assert [j.index for j in judgments] == [0, 1, 2, 3]


def test_default_chunk_is_30_and_configurable(monkeypatch):
    sizes = []

    def fake(client, model, system_prompt, contents, schema):
        sizes.append(len(_payload(contents)))
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, fake)
    judge_segments(_segments(65), caller=_caller())
    assert sizes == [30, 30, 5]
    sizes.clear()
    monkeypatch.setenv("GEMINI_MAX_SEGMENTS_PER_CALL", "10")
    get_settings.cache_clear()
    judge_segments(_segments(25), caller=_caller())
    assert sizes == [10, 10, 5]


def test_progress_reports_segments_done(monkeypatch):
    _fake(monkeypatch, lambda c, m, sp, contents, schema: _FakeResponse(_answer(contents)))
    progress = []
    judge_segments(_segments(65), caller=_caller(), on_progress=lambda c, t: progress.append((c, t)))
    assert progress == [(30, 65), (60, 65), (65, 65)]


def test_saved_decisions_are_reused_and_resume_mid_way(monkeypatch, tmp_path):
    path = tmp_path / "decisions.json"
    calls = []

    def fake(client, model, system_prompt, contents, schema):
        calls.append(len(_payload(contents)))
        if len(calls) == 2:
            raise genai_errors.ClientError(400, {"error": {"message": "boom"}})
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, fake)
    with pytest.raises(DecisionError):
        judge_segments(_segments(65), caller=_caller(), persist_path=path)
    assert len(json.loads(path.read_text())["judgments"]) == 30  # first chunk saved

    calls.clear()
    _fake(monkeypatch, lambda c, m, sp, contents, schema: (calls.append(len(_payload(contents))),
                                                              _FakeResponse(_answer(contents)))[1])
    judgments = judge_segments(_segments(65), caller=_caller(), persist_path=path)
    assert calls == [30, 5]  # the saved chunk is not judged again
    assert len(judgments) == 65

    calls.clear()
    judge_segments(_segments(65), caller=_caller(), persist_path=path)
    assert calls == []  # everything saved now

    judge_segments(_segments(65), caller=_caller(), persist_path=path, highlights_criteria="other")
    assert calls == [30, 30, 5]  # different criteria -> different prompt -> judged afresh


def test_get_decisions_wrapper(monkeypatch):
    _fake(monkeypatch, lambda c, m, sp, contents, schema: _FakeResponse(_answer(contents, decision="remove")))
    decisions = get_decisions(_segments(), caller=_caller())
    assert [(d.start, d.decision) for d in decisions] == [(0.0, "remove"), (1.0, "remove")]


def test_empty_segments_short_circuit(monkeypatch):
    _fake(monkeypatch, lambda *a: (_ for _ in ()).throw(AssertionError("should not be called")))
    assert judge_segments([], caller=_caller()) == []


# ---------------------------------------------------------------- caller


def test_429_waits_for_the_servers_retry_delay(monkeypatch, _fake_api_key):
    sleeps = _fake_api_key
    calls = {"n": 0}

    def fake(client, model, system_prompt, contents, schema):
        calls["n"] += 1
        if calls["n"] == 1:
            raise genai_errors.ClientError(429, {"error": {"status": "RESOURCE_EXHAUSTED", "details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}})
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, fake)
    caller = _caller()
    judge_segments(_segments(), caller=caller)
    assert sleeps == [7.0]
    assert caller.usage.retries == 1 and caller.usage.rate_limit_wait_s == 7.0


def test_429_without_details_uses_default_wait(monkeypatch, _fake_api_key):
    sleeps = _fake_api_key
    calls = {"n": 0}

    def fake(client, model, system_prompt, contents, schema):
        calls["n"] += 1
        if calls["n"] == 1:
            raise genai_errors.ClientError(429, {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}})
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, fake)
    judge_segments(_segments(), caller=_caller())
    assert sleeps and sleeps[0] == gemini_client.DEFAULT_429_WAIT_S


def test_daily_quota_fails_fast_and_retryable(monkeypatch):
    def fake(*a):
        raise genai_errors.ClientError(429, {"error": {"details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "5000s"}]}})

    _fake(monkeypatch, fake)
    with pytest.raises(PipelineError) as info:
        judge_segments(_segments(), caller=_caller())
    assert info.value.code == "llm_quota_exhausted"
    assert info.value.retryable and info.value.retry_after_s == 5000


def test_server_error_is_retried_client_error_is_not(monkeypatch, _fake_api_key):
    calls = {"n": 0}

    def flaky(client, model, system_prompt, contents, schema):
        calls["n"] += 1
        if calls["n"] == 1:
            raise genai_errors.ServerError(503, {"error": {"message": "overloaded"}})
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, flaky)
    judge_segments(_segments(), caller=_caller())
    assert calls["n"] == 2

    calls["n"] = 0

    def bad_request(*a):
        calls["n"] += 1
        raise genai_errors.ClientError(400, {"error": {"message": "API key not valid"}})

    _fake(monkeypatch, bad_request)
    with pytest.raises(DecisionError):
        judge_segments(_segments(), caller=_caller())
    assert calls["n"] == 1


def test_rate_limiter_spaces_requests(monkeypatch, _fake_api_key):
    sleeps = _fake_api_key
    limiter = RateLimiter(rpm=2)
    assert limiter.acquire() == 0.0
    assert limiter.acquire() == 0.0
    waited = limiter.acquire()
    assert 59.0 < waited <= 60.0 and sleeps == [waited]


def test_wall_clock_limit(monkeypatch):
    _fake(monkeypatch, lambda c, m, sp, contents, schema: _FakeResponse(_answer(contents)))
    caller = _caller(max_wall_s=1)
    caller.deadline = 0.0  # already past
    with pytest.raises(PipelineError) as info:
        judge_segments(_segments(), caller=caller)
    assert info.value.code == "timeout" and info.value.retryable


def test_usage_is_counted(monkeypatch):
    _fake(monkeypatch, lambda c, m, sp, contents, schema: _FakeResponse(_answer(contents)))
    caller = _caller()
    judge_segments(_segments(65), caller=caller)
    assert caller.usage.as_dict()["calls"] == 3
    assert caller.usage.prompt_tokens == 300 and caller.usage.output_tokens == 150


# ---------------------------------------------------------------- re-rank


def _judgments(n=12):
    from core.models import SegmentJudgment

    return [SegmentJudgment(index=i, start=i * 5.0, end=i * 5.0 + 4.5, speaker="A", text=f"sentence {i}",
                            decision="keep", highlight_score=8 if i in (4, 5) else 1,
                            highlight_category="funny" if i in (4, 5) else "none") for i in range(n)]


def test_rerank_updates_scores_titles_and_windows(monkeypatch):
    judgments = _judgments()
    cand = Moment(id=0, start=20.0, end=29.5, first_index=4, last_index=5, score=8, category="funny")
    answer = {"moments": [{"id": 0, "score": 9, "category": "funny", "title": 'The <b>"coffee"</b> incident\n',
                           "hook": "x" * 300, "first_index": 3, "last_index": 99, "short_worthy": True}]}
    _fake(monkeypatch, lambda *a: _FakeResponse(json.dumps(answer)))
    result = rerank_moments([cand], judgments, caller=_caller(), context_segments=2)
    (m,) = result.moments
    assert result.ok and m.reranked and m.short_worthy
    assert m.score == 9.0
    assert (m.first_index, m.last_index) == (3, 7)  # 99 clamped to last_index + context
    assert (m.start, m.end) == (15.0, 39.5)
    assert "<" not in m.title and '"' not in m.title and len(m.hook) <= 140


def test_rerank_window_must_overlap_the_scored_core(monkeypatch):
    judgments = _judgments()
    cand = Moment(id=0, start=20.0, end=29.5, first_index=4, last_index=5, score=8, category="funny")
    answer = {"moments": [{"id": 0, "score": 6, "category": "funny", "title": "t", "hook": "h",
                           "first_index": 2, "last_index": 3, "short_worthy": False}]}
    _fake(monkeypatch, lambda *a: _FakeResponse(json.dumps(answer)))
    (m,) = rerank_moments([cand], judgments, caller=_caller()).moments
    assert (m.first_index, m.last_index) == (4, 5)


def test_rerank_failure_falls_back_to_segment_scores(monkeypatch):
    judgments = _judgments()
    cand = Moment(id=0, start=20.0, end=29.5, first_index=4, last_index=5, score=8, category="funny")
    _fake(monkeypatch, lambda *a: _FakeResponse("nope"))
    result = rerank_moments([cand], judgments, caller=_caller())
    assert not result.ok and result.moments == [cand] and "failed" in result.note


def test_rerank_notes_missing_candidates(monkeypatch):
    judgments = _judgments()
    cands = [Moment(id=k, start=20.0 + k, end=29.5, first_index=4, last_index=5, score=8) for k in range(2)]
    answer = {"moments": [{"id": 1, "score": 5, "category": "concept", "title": "t", "hook": "h",
                           "first_index": 4, "last_index": 5, "short_worthy": False}]}
    _fake(monkeypatch, lambda *a: _FakeResponse(json.dumps(answer)))
    result = rerank_moments(cands, judgments, caller=_caller())
    assert result.moments[0] == cands[0] and result.moments[1].reranked
    assert "1 candidate" in result.note
