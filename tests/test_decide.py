import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from google.genai import errors as genai_errors

from core.config import get_settings
from core.errors import PipelineError, classify
from core.models import Moment, Segment, SegmentJudgment
from decide import gemini_client
from decide.gemini_client import (
    DecisionError,
    GeminiCaller,
    RateLimiter,
    complete_window,
    get_decisions,
    judge_segments,
    rerank_moments,
    seconds_until_quota_reset,
)
from decide.prompts import RESCORE_NOTE


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


def _judged_lines(contents: str) -> list[int]:
    """Line numbers under JUDGE (not the CONTEXT lines)."""
    section = contents.split("JUDGE:", 1)[1].split("CONTEXT", 1)[0]
    return [int(n) for n in re.findall(r"^\[(\d+)\]", section, re.MULTILINE)]


def _items(contents, d="k", **overrides):
    items = []
    for n in _judged_lines(contents):
        item = {"i": n, "d": d, "s": 2, "c": "-", "h": "-"}
        item.update(overrides)
        items.append(item)
    return items


def _lines(items):
    return "\n".join(f"{x['i']} {x['d']} {x['s']} {x['c']} {x['h']}" for x in items)


def _answer(contents, d="k", **overrides):
    return _lines(_items(contents, d, **overrides))


def _fake(monkeypatch, fn):
    monkeypatch.setattr(gemini_client, "_call_gemini", fn)


def _echo(**overrides):
    return lambda c, m, sp, contents, schema: _FakeResponse(_answer(contents, **overrides))


# ---------------------------------------------------------------- judging


def test_compact_answer_is_mapped_to_full_judgments(monkeypatch):
    _fake(monkeypatch, _echo(d="r", c="greet", s=7, h="fun"))
    judgments = judge_segments(_segments(3), caller=_caller())
    assert [j.index for j in judgments] == [0, 1, 2]
    assert judgments[1].text == "yeah yeah 1" and judgments[1].start == 1.0 and judgments[1].speaker == "B"
    assert all(j.decision == "remove" and j.removal_category == "greeting_small_talk" for j in judgments)
    assert all(j.highlight_score == 7 and j.highlight_category == "funny" for j in judgments)


def test_keep_ignores_removal_code_and_low_scores_drop_category(monkeypatch):
    _fake(monkeypatch, _echo(c="fill", s=2, h="arch"))
    for j in judge_segments(_segments(), caller=_caller()):
        assert j.removal_category == "none" and j.highlight_category == "none"


def test_a_joke_keeps_its_tag_from_score_two(monkeypatch):
    _fake(monkeypatch, _echo(s=2, h="fun"))
    assert all(j.highlight_category == "funny" for j in judge_segments(_segments(), caller=_caller()))


def test_prompt_is_compact_text_with_criteria_and_speaker_changes(monkeypatch):
    seen = {}

    def fake(client, model, system_prompt, contents, schema):
        seen.update(system=system_prompt, schema=schema, contents=contents)
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, fake)
    segs = [Segment(speaker="Ann", start=0, end=1, text="hello"), Segment(speaker="Ann", start=1, end=2, text="more"),
            Segment(speaker="Bo", start=2, end=3, text="hi", overlap_candidate=True)]
    judge_segments(segs, caller=_caller(), highlights_criteria="dragons and spaceships")
    assert "dragons and spaceships" in seen["system"]
    assert seen["schema"] is None  # plain-text answers: JSON was pretty-printed at ~4x the tokens
    assert seen["contents"].splitlines() == ["JUDGE:", "[0] Ann: hello", "[1] more", "[2] Bo: hi (overlap)"]


def test_chunks_carry_read_only_context(monkeypatch):
    contents_seen = []

    def fake(client, model, system_prompt, contents, schema):
        contents_seen.append(contents)
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, fake)
    judge_segments(_segments(65), caller=_caller())
    first, second = contents_seen
    assert _judged_lines(first) == list(range(60)) and "[60]" in first.split("JUDGE:")[1]  # context after
    assert first.startswith("JUDGE:")  # nothing before the first line
    assert second.startswith("CONTEXT") and "[57]" in second.split("JUDGE:")[0]
    assert _judged_lines(second) == list(range(60, 65))


def test_answers_for_context_lines_are_ignored_without_a_retry(monkeypatch):
    calls = []

    def fake(client, model, system_prompt, contents, schema):
        calls.append(contents)
        items = _items(contents)
        context = [int(n) for n in re.findall(r"^~\[(\d+)\]", contents, re.MULTILINE)]
        items += [{"i": n, "d": "r", "s": 0, "c": "fill", "h": "-"} for n in context]  # judged the context too
        return _FakeResponse(_lines(items))

    _fake(monkeypatch, fake)
    judgments = judge_segments(_segments(65), caller=_caller())
    assert len(calls) == 2 and "~[60]" in calls[0]
    assert all(j.decision == "keep" for j in judgments)  # the context answers did not leak in


def test_retries_once_on_malformed_json_then_succeeds(monkeypatch):
    calls = []

    def fake(client, model, system_prompt, contents, schema):
        calls.append(contents)
        return _FakeResponse("not json" if len(calls) == 1 else _answer(contents))

    _fake(monkeypatch, fake)
    assert len(judge_segments(_segments(), caller=_caller())) == 2
    assert len(calls) == 2 and calls[1].startswith("Your previous answer was not valid")


def test_fails_loudly_when_a_single_segment_never_parses(monkeypatch):
    _fake(monkeypatch, lambda *a: _FakeResponse("still not json"))
    with pytest.raises(DecisionError):
        judge_segments(_segments(1), caller=_caller())


def test_truncation_splits_instead_of_blind_retry(monkeypatch, caplog):
    sizes = []

    def fake(client, model, system_prompt, contents, schema):
        n = len(_judged_lines(contents))
        sizes.append(n)
        if n > 2:
            return _FakeResponse("0 k 2 - -\n1 k", finish_reason="MAX_TOKENS")
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
        if len(_judged_lines(contents)) == 8:
            attempts_at_8.append(1)
            lines = _answer(contents).splitlines()
            lines[1] = "1 2 - -"  # the decision is missing on one line
            return _FakeResponse("\n".join(lines), finish_reason="STOP")
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, fake)
    assert len(judge_segments(_segments(8), caller=_caller())) == 8
    assert len(attempts_at_8) == 2


@pytest.mark.parametrize("mutate", ["wrong_index", "duplicate", "missing", "bad_decision", "bad_score"])
def test_index_set_must_match_exactly(monkeypatch, mutate):
    """A response that judges the wrong lines must never be accepted — that
    is what could silently mis-assign keep/remove after a split."""
    bad = {"n": 0}

    def fake(client, model, system_prompt, contents, schema):
        items = _items(contents)
        if bad["n"] < 2 and len(items) > 1:
            bad["n"] += 1
            if mutate == "wrong_index":
                items[0]["i"] = 999
            elif mutate == "duplicate":
                items[1]["i"] = items[0]["i"]
            elif mutate == "missing":
                items.pop()
            elif mutate == "bad_decision":
                items[0]["d"] = "maybe"
            else:
                items[0]["s"] = 11
        return _FakeResponse(_lines(items))

    _fake(monkeypatch, fake)
    assert [j.index for j in judge_segments(_segments(4), caller=_caller())] == [0, 1, 2, 3]


def test_default_chunk_is_60_and_configurable(monkeypatch):
    sizes = []

    def fake(client, model, system_prompt, contents, schema):
        sizes.append(len(_judged_lines(contents)))
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, fake)
    judge_segments(_segments(130), caller=_caller())
    assert sizes == [60, 60, 10]
    sizes.clear()
    monkeypatch.setenv("GEMINI_MAX_SEGMENTS_PER_CALL", "10")
    get_settings.cache_clear()
    judge_segments(_segments(25), caller=_caller())
    assert sizes == [10, 10, 5]


def test_progress_reports_segments_done(monkeypatch):
    _fake(monkeypatch, _echo())
    progress = []
    judge_segments(_segments(130), caller=_caller(), on_progress=lambda c, t: progress.append((c, t)))
    assert progress == [(60, 130), (120, 130), (130, 130)]


def test_collapsed_scores_are_rescored_once(monkeypatch):
    calls = []

    def fake(client, model, system_prompt, contents, schema):
        calls.append(contents)
        if contents.startswith(RESCORE_NOTE):
            return _FakeResponse(_answer(contents, s=4, h="idea"))
        return _FakeResponse(_answer(contents, s=0))

    _fake(monkeypatch, fake)
    judgments = judge_segments(_segments(20), caller=_caller())
    assert len(calls) == 2
    assert all(j.highlight_score == 4 for j in judgments)


def test_rescore_is_capped_and_needs_enough_kept_lines(monkeypatch):
    calls = []

    def fake(client, model, system_prompt, contents, schema):
        calls.append(contents)
        return _FakeResponse(_answer(contents, s=0))

    _fake(monkeypatch, fake)
    monkeypatch.setenv("GEMINI_MAX_SEGMENTS_PER_CALL", "15")
    monkeypatch.setenv("GEMINI_MAX_RESCORES", "2")
    get_settings.cache_clear()
    judge_segments(_segments(75), caller=_caller())  # 5 collapsed chunks, only 2 re-scored
    assert sum(c.startswith(RESCORE_NOTE) for c in calls) == 2
    calls.clear()
    judge_segments(_segments(5), caller=_caller())  # too few lines to call it a collapse
    assert len(calls) == 1


def test_saved_decisions_are_reused_and_resume_mid_way(monkeypatch, tmp_path):
    path = tmp_path / "decisions.json"
    calls = []

    def fake(client, model, system_prompt, contents, schema):
        calls.append(len(_judged_lines(contents)))
        if len(calls) == 2:
            raise genai_errors.ClientError(400, {"error": {"message": "boom"}})
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, fake)
    with pytest.raises(DecisionError):
        judge_segments(_segments(130), caller=_caller(), persist_path=path)
    assert len(json.loads(path.read_text())["judgments"]) == 60  # first chunk saved

    calls.clear()
    _fake(monkeypatch, lambda c, m, sp, contents, schema: (calls.append(len(_judged_lines(contents))),
                                                              _FakeResponse(_answer(contents)))[1])
    judgments = judge_segments(_segments(130), caller=_caller(), persist_path=path)
    assert calls == [60, 10]  # the saved chunk is not judged again
    assert len(judgments) == 130

    calls.clear()
    judge_segments(_segments(130), caller=_caller(), persist_path=path)
    assert calls == []  # everything saved now

    judge_segments(_segments(130), caller=_caller(), persist_path=path, highlights_criteria="other")
    assert calls == [60, 60, 10]  # different criteria -> different prompt -> judged afresh


def test_get_decisions_wrapper(monkeypatch):
    _fake(monkeypatch, _echo(d="r"))
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


def test_daily_quota_fails_fast_and_retries_after_the_reset(monkeypatch):
    def fake(*a):
        raise genai_errors.ClientError(429, {"error": {"details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "5s"}]}})

    _fake(monkeypatch, fake)
    with pytest.raises(PipelineError) as info:
        judge_segments(_segments(), caller=_caller())
    assert info.value.code == "llm_quota_exhausted" and info.value.retryable
    assert 60 <= info.value.retry_after_s <= 24 * 3600  # until midnight Pacific, not the 5 s hint


def test_quota_reset_is_next_midnight_pacific():
    tz = ZoneInfo("America/Los_Angeles")
    assert seconds_until_quota_reset(datetime(2026, 9, 25, 23, 0, tzinfo=tz)) == 3600
    assert seconds_until_quota_reset(datetime(2026, 9, 25, 12, 0, tzinfo=tz)) == 12 * 3600


def test_server_and_network_errors_are_retried(monkeypatch):
    calls = {"n": 0}

    def flaky(client, model, system_prompt, contents, schema):
        calls["n"] += 1
        if calls["n"] == 1:
            raise genai_errors.ServerError(503, {"error": {"message": "overloaded"}})
        if calls["n"] == 2:
            raise httpx.ConnectError("connection reset")
        return _FakeResponse(_answer(contents))

    _fake(monkeypatch, flaky)
    judge_segments(_segments(), caller=_caller())
    assert calls["n"] == 3


def test_network_error_that_persists_is_a_retryable_decision_error(monkeypatch):
    def down(*a):
        raise httpx.ReadTimeout("stalled")

    _fake(monkeypatch, down)
    with pytest.raises(DecisionError) as info:
        judge_segments(_segments(), caller=_caller())
    assert info.value.retryable
    assert classify(info.value) == ("llm_error", True, None)


def test_permanent_client_errors_are_not_retried_or_retryable(monkeypatch):
    calls = {"n": 0}

    def bad_request(*a):
        calls["n"] += 1
        raise genai_errors.ClientError(400, {"error": {"message": "API key not valid"}})

    _fake(monkeypatch, bad_request)
    with pytest.raises(DecisionError) as info:
        judge_segments(_segments(), caller=_caller())
    assert calls["n"] == 1
    assert classify(info.value) == ("llm_error", False, None)


def test_missing_api_key_is_not_retryable(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(DecisionError) as info:
        gemini_client.make_caller()
    assert not info.value.retryable


def test_rate_limiter_spaces_requests(monkeypatch, _fake_api_key):
    sleeps = _fake_api_key
    limiter = RateLimiter(rpm=2)
    assert limiter.acquire() == 0.0
    assert limiter.acquire() == 0.0
    waited = limiter.acquire()
    assert 59.0 < waited <= 60.0 and sleeps == [waited]


def test_stage_deadlines(monkeypatch):
    _fake(monkeypatch, _echo())
    caller = _caller(max_wall_s=1)
    caller.deadline = 0.0  # already past
    with pytest.raises(PipelineError) as info:
        judge_segments(_segments(), caller=caller)
    assert info.value.code == "timeout" and info.value.retryable
    caller.start_stage("chapters", 300)  # a new stage gets a fresh budget
    assert len(judge_segments(_segments(), caller=caller)) == 2


def test_usage_is_counted(monkeypatch):
    _fake(monkeypatch, _echo())
    caller = _caller()
    judge_segments(_segments(130), caller=caller)
    assert caller.usage.as_dict()["calls"] == 3
    assert caller.usage.prompt_tokens == 300 and caller.usage.output_tokens == 150


# ---------------------------------------------------------------- re-rank


def _judgments(n=12, texts=None):
    texts = texts or {}
    return [SegmentJudgment(index=i, start=i * 5.0, end=i * 5.0 + 4.5, speaker="A", text=texts.get(i, f"Sentence {i}."),
                            decision="keep", highlight_score=8 if i in (4, 5) else 1,
                            highlight_category="funny" if i in (4, 5) else "none") for i in range(n)]


def _cand(**kw):
    base = {"id": 0, "start": 20.0, "end": 29.5, "first_index": 4, "last_index": 5, "score": 8,
            "category": "funny", "peak_index": 4}
    base.update(kw)
    return Moment(**base)


def test_rerank_updates_scores_titles_and_windows(monkeypatch):
    judgments = _judgments()
    answer = "0|9|fun|3|99|y|The <b>coffee</b> incident|" + "x" * 300
    seen = {}

    def fake(client, model, system_prompt, contents, schema):
        seen["contents"] = contents
        return _FakeResponse(answer)

    _fake(monkeypatch, fake)
    result = rerank_moments([_cand()], judgments, caller=_caller(), context_segments=2)
    (m,) = result.moments
    assert result.ok and m.reranked and m.short_worthy and m.score == 9.0
    assert (m.first_index, m.last_index) == (3, 7)  # 99 clamped to last_index + context
    assert (m.start, m.end) == (15.0, 39.5)
    assert "<" not in m.title and len(m.hook) <= 140
    assert seen["contents"].splitlines()[:2] == ["#0 core 4-5", "~[2] A: Sentence 2."]


def test_rerank_window_must_overlap_the_scored_core(monkeypatch):
    _fake(monkeypatch, lambda *a: _FakeResponse("0|6|fun|2|3|n|t|h"))
    (m,) = rerank_moments([_cand()], _judgments(), caller=_caller()).moments
    assert (m.first_index, m.last_index) == (4, 5)


def test_windows_never_start_on_a_connective_or_end_mid_sentence(monkeypatch):
    judgments = _judgments(texts={3: "We tried Redis first.", 4: "which cut latency", 5: "to a third", 6: "of what it was."})
    _fake(monkeypatch, lambda *a: _FakeResponse("0|7|arch|4|5|n|t|h"))
    (m,) = rerank_moments([_cand()], judgments, caller=_caller()).moments
    assert (m.first_index, m.last_index) == (3, 6)


def test_rerank_lines_drop_stray_pipes_and_accept_a_missing_hook():
    from decide.gemini_client import parse_rerank_lines

    items = parse_rerank_lines("0|7|fun|4|5|y|Coffee |Thanks for sharing.|\n1|6|arch|4|5|n|Title only|\n2|5|dec|4|5|n|T")
    assert [(x["id"], x["t"], x["k"]) for x in items] == [
        (0, "Coffee", "Thanks for sharing."), (1, "Title only", ""), (2, "T", "")]


def test_complete_window_limits():
    judgments = _judgments(texts={i: "and more" for i in range(12)})
    assert complete_window(5, 6, judgments, 4, 7) == (4, 7)  # never past the limits
    assert complete_window(5, 6, judgments, 0, 11, max_steps=2) == (3, 8)


def test_rerank_failure_falls_back_to_segment_scores(monkeypatch):
    _fake(monkeypatch, lambda *a: _FakeResponse("nope"))
    result = rerank_moments([_cand()], _judgments(), caller=_caller())
    assert not result.ok and result.moments == [_cand()] and "failed" in result.note


def test_rerank_api_error_is_not_fatal(monkeypatch):
    def boom(*a):
        raise genai_errors.ClientError(400, {"error": {"message": "bad"}})

    _fake(monkeypatch, boom)
    assert not rerank_moments([_cand()], _judgments(), caller=_caller()).ok


def test_rerank_asks_again_only_for_skipped_candidates(monkeypatch):
    cands = [_cand(id=k, start=20.0 + k) for k in range(3)]
    calls = []

    def fake(client, model, system_prompt, contents, schema):
        calls.append(contents)
        if len(calls) == 1:
            return _FakeResponse("1|5|idea|4|5|n|t|h")  # skipped 0 and 2
        assert "#1 " not in contents and "#0 " in contents and "#2 " in contents
        return _FakeResponse("2|3|dec|4|5|n|t2|h2")  # still skips 0

    _fake(monkeypatch, fake)
    result = rerank_moments(cands, _judgments(), caller=_caller())
    assert len(calls) == 2
    assert result.moments[0] == cands[0] and result.moments[1].reranked and result.moments[2].reranked
    assert result.moments[1].category == "concept" and result.moments[2].category == "decision"
    assert "1 candidate" in result.note


def test_a_dropped_placeholder_is_tolerated_but_unknown_codes_are_not():
    from decide.gemini_client import parse_judge_lines

    items = parse_judge_lines("65 k 4 -\n120 k 5 arch\n7 r 0 fill\n8 r 0 fill fun\n9 k 2\n[10] k 6 - dec")
    assert [(x["i"], x["c"], x["h"]) for x in items] == [
        (65, "-", "-"), (120, "-", "arch"), (7, "fill", "-"), (8, "fill", "fun"), (9, "-", "-"), (10, "-", "dec")]
    with pytest.raises(ValueError):
        parse_judge_lines("10 k 5 banana")
    with pytest.raises(ValueError):
        parse_judge_lines("10 maybe 5 - -")
