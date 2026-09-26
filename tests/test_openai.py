import json
import logging
import re
import traceback
from types import SimpleNamespace

import httpx
import pytest

from core.config import Settings, get_settings
from core.errors import PipelineError, classify
from core.models import Moment, Segment, SegmentJudgment, Word
from decide import gemini_client, openai_client
from decide.chapters import generate_chapters
from decide.gemini_client import (
    DEFAULT_429_WAIT_S,
    MAX_429_WAIT_S,
    MAX_RATE_LIMIT_RETRIES,
    MAX_SERVER_ERROR_RETRIES,
    DecisionError,
    GeminiCaller,
    LazyCaller,
    judge_segments,
    make_caller,
    rerank_moments,
)
from decide.openai_client import OpenAICaller, parse_reset

KEY = "sk-test-0123456789abcdefSECRET"
URL = "https://api.openai.com/v1/responses"


@pytest.fixture(autouse=True)
def _openai_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    for name in ("OPENAI_MODEL", "OPENAI_REASONING_EFFORT", "OPENAI_BASE_URL", "OPENAI_MAX_OUTPUT_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    sleeps = []
    monkeypatch.setattr(openai_client.time, "sleep", lambda s: sleeps.append(s))
    yield sleeps
    get_settings.cache_clear()


def _body(text, status="completed", reason=None, usage=None, content=None):
    return {
        "id": "resp_1",
        "object": "response",
        "status": status,
        "incomplete_details": {"reason": reason} if reason else None,
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "message", "role": "assistant",
             "content": content if content is not None else [{"type": "output_text", "text": text, "annotations": []}]},
        ],
        "usage": usage or {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 50,
                           "output_tokens_details": {"reasoning_tokens": 0}},
    }


def _resp(status=200, body=None, headers=None):
    return httpx.Response(status, json=body, headers=headers, request=httpx.Request("POST", URL))


def _error(status, message, code=None, kind="invalid_request_error", headers=None):
    return _resp(status, {"error": {"message": message, "type": kind, "param": None, "code": code}}, headers)


def _caller(**kw):
    return OpenAICaller(KEY, "test-model", 1000, **kw)


def _fake(monkeypatch, fn):
    """fn(body) -> httpx.Response (or raises); records every request body."""
    bodies = []

    def call(client, body):
        bodies.append(body)
        return fn(body)

    monkeypatch.setattr(openai_client, "_call_openai", call)
    return bodies


def _segments(n=2):
    return [Segment(speaker="A", start=float(i), end=i + 1.0, text=f"sentence {i}") for i in range(n)]


def _judged(contents: str) -> list[int]:
    section = contents.split("JUDGE:", 1)[1].split("CONTEXT", 1)[0]
    return [int(n) for n in re.findall(r"^\[(\d+)\]", section, re.MULTILINE)]


def _answer(contents: str) -> str:
    return "\n".join(f"{n} k 4 - idea" for n in _judged(contents))


# ---------------------------------------------------------------- request / response


def test_request_body_headers_and_endpoint():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_body("ok"))

    caller = _caller(transport=httpx.MockTransport(handler), max_output_tokens=900, reasoning_effort="none")
    assert caller.generate("SYSTEM", "hello").text == "ok"
    (req,) = seen
    assert str(req.url) == URL and req.method == "POST"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(req.content) == {"model": "test-model", "instructions": "SYSTEM", "input": "hello",
                                       "store": False, "max_output_tokens": 900, "reasoning": {"effort": "none"}}


def test_empty_reasoning_effort_omits_the_key_and_schema_asks_for_json():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_body("{}"))

    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    _caller(transport=httpx.MockTransport(handler), reasoning_effort="").generate("s", "c", schema)
    assert "reasoning" not in seen[0]
    assert seen[0]["text"] == {"format": {"type": "json_schema", "name": "answer", "schema": schema, "strict": False}}


def test_text_and_usage_are_parsed(monkeypatch):
    usage = {"input_tokens": 1200, "input_tokens_details": {"cached_tokens": 1024}, "output_tokens": 300,
             "output_tokens_details": {"reasoning_tokens": 0}}
    parts = [{"type": "output_text", "text": "0 k 4 - -\n", "annotations": []},
             {"type": "output_text", "text": "1 r 0 fill -", "annotations": []}]
    _fake(monkeypatch, lambda body: _resp(200, _body("", usage=usage, content=parts)))
    caller = _caller()
    response = caller.generate("s", "c")
    assert response.text == "0 k 4 - -\n1 r 0 fill -" and response.finish_reason == "completed"
    assert caller.usage.as_dict() == {"calls": 1, "prompt_tokens": 1200, "output_tokens": 300,
                                      "rate_limit_wait_s": 0.0, "retries": 0}


def test_judging_runs_unchanged_on_the_openai_caller(monkeypatch):
    bodies = _fake(monkeypatch, lambda body: _resp(200, _body(_answer(body["input"]))))
    judgments = judge_segments(_segments(5), caller=_caller())
    assert [j.index for j in judgments] == list(range(5))
    assert all(j.decision == "keep" and j.highlight_category == "concept" for j in judgments)
    assert len(bodies) == 1 and "sentence 3" in bodies[0]["input"] and "LEARN" in bodies[0]["instructions"]


def test_truncated_answer_splits_the_chunk(monkeypatch, caplog):
    sizes = []

    def fn(body):
        n = len(_judged(body["input"]))
        sizes.append(n)
        if n > 2:
            return _resp(200, _body("0 k 4 - -\n1 k", status="incomplete", reason="max_output_tokens"))
        return _resp(200, _body(_answer(body["input"])))

    _fake(monkeypatch, fn)
    with caplog.at_level(logging.WARNING):
        judgments = judge_segments(_segments(8), caller=_caller())
    assert [j.index for j in judgments] == list(range(8))
    assert sizes.count(8) == 1 and sizes.count(2) >= 1  # split at once, no same-size retry
    assert any("MAX_TOKENS" in r.message for r in caplog.records)


def test_other_incomplete_reasons_are_not_mistaken_for_truncation():
    parsed = openai_client.parse_response(_body("x", status="incomplete", reason="content_filter"))
    assert parsed.finish_reason == "incomplete"


def test_a_refusal_is_an_invalid_answer_and_is_retried(monkeypatch):
    refusal = [{"type": "refusal", "refusal": "I can't help with that."}]
    calls = []

    def fn(body):
        calls.append(body["input"])
        if len(calls) == 1:
            return _resp(200, _body("", content=refusal))
        return _resp(200, _body(_answer(body["input"])))

    _fake(monkeypatch, fn)
    assert len(judge_segments(_segments(2), caller=_caller())) == 2
    assert len(calls) == 2 and calls[1].startswith("Your previous answer was not valid")


def test_rerank_and_chapters_work_on_the_openai_caller(monkeypatch):
    answers = iter(["0|8|arch|4|5|y|Caching layer|How the cache cuts latency",
                    "0|Intro\n25|Roadmap\n48|Hiring\n80|Wrap-up"])
    _fake(monkeypatch, lambda body: _resp(200, _body(next(answers))))
    caller = _caller()
    judgments = [SegmentJudgment(index=i, start=i * 5.0, end=i * 5.0 + 4.5, speaker="A", text=f"Sentence {i}.",
                                 decision="keep", highlight_score=8 if i in (4, 5) else 1) for i in range(12)]
    moment = Moment(id=0, start=20.0, end=29.5, first_index=4, last_index=5, score=8, category="concept")
    result = rerank_moments([moment], judgments, caller=caller)
    assert result.ok and result.moments[0].title == "Caching layer" and result.moments[0].short_worthy
    words =[Word(word=f"sentence {i}.", start=i * 10.0, end=i * 10.0 + 8, speaker="A") for i in range(120)]
    caller.start_stage("chapters", 300)
    assert generate_chapters(words, 1200, caller=caller).ok
    assert caller.usage.calls == 2


# ---------------------------------------------------------------- errors


@pytest.mark.parametrize("code,kind", [("insufficient_quota", "insufficient_quota"), (None, "insufficient_quota")])
def test_no_credit_fails_fast_and_is_not_retryable(monkeypatch, _openai_env, code, kind):
    sleeps = _openai_env
    bodies = _fake(monkeypatch, lambda body: _error(
        429, "You exceeded your current quota, please check your plan and billing details.", code=code, kind=kind))
    with pytest.raises(PipelineError) as info:
        judge_segments(_segments(), caller=_caller())
    assert info.value.code == "llm_quota_exhausted" and not info.value.retryable
    assert "platform.openai.com" in str(info.value) and "no credit" in str(info.value)
    assert classify(info.value) == ("llm_quota_exhausted", False, None)
    assert len(bodies) == 1 and sleeps == []


@pytest.mark.parametrize("headers,message,expected", [
    ({"retry-after-ms": "2500", "retry-after": "9"}, "", 2.5),
    ({"retry-after": "3"}, "", 3.0),
    ({"x-ratelimit-reset-requests": "1s", "x-ratelimit-reset-tokens": "6m0s"}, "", MAX_429_WAIT_S),
    ({"x-ratelimit-reset-requests": "120ms", "x-ratelimit-reset-tokens": "1.5s"}, "", 1.5),
    ({}, "Rate limit reached for test-model on tokens per min (TPM). Please try again in 4.2s. Visit ...", 4.2),
    ({"retry-after-ms": "50"}, "", 1.0),  # never hammer the API with sub-second retries
    ({}, "Rate limit reached.", DEFAULT_429_WAIT_S),
])
def test_rate_limit_waits_for_what_the_server_asks(monkeypatch, _openai_env, headers, message, expected):
    sleeps = _openai_env
    calls = []

    def fn(body):
        calls.append(1)
        if len(calls) == 1:
            return _error(429, message, code="rate_limit_exceeded", kind="requests", headers=headers)
        return _resp(200, _body("fine"))

    _fake(monkeypatch, fn)
    caller = _caller()
    assert caller.generate("s", "c").text == "fine"
    assert sleeps == [pytest.approx(expected)]
    assert caller.usage.retries == 1 and caller.usage.rate_limit_wait_s == pytest.approx(expected)


def test_reset_durations():
    assert parse_reset("1s") == 1.0 and parse_reset("6m0s") == 360.0
    assert parse_reset("120ms") == pytest.approx(0.12) and parse_reset("1h2m3.5s") == pytest.approx(3723.5)
    assert parse_reset("2") == 2.0 and parse_reset("soon") is None and parse_reset(None) is None


def test_endless_rate_limiting_gives_up_retryably(monkeypatch):
    bodies = _fake(monkeypatch, lambda body: _error(429, "slow down", code="rate_limit_exceeded",
                                                    headers={"retry-after": "2"}))
    with pytest.raises(DecisionError) as info:
        _caller().generate("s", "c")
    assert info.value.retryable and "kept rate-limiting" in str(info.value)
    assert len(bodies) == MAX_RATE_LIMIT_RETRIES + 1


def test_server_errors_are_retried_with_backoff(monkeypatch, _openai_env):
    sleeps = _openai_env
    replies = iter([_error(503, "overloaded", kind="server_error"), _error(408, "timeout"),
                    _resp(200, _body("fine"))])
    _fake(monkeypatch, lambda body: next(replies))
    caller = _caller()
    assert caller.generate("s", "c").text == "fine"
    assert sleeps == [5.0, 10.0] and caller.usage.retries == 2 and caller.usage.calls == 1


def test_persistent_server_error_is_a_retryable_decision_error(monkeypatch):
    bodies = _fake(monkeypatch, lambda body: _error(500, "internal", kind="server_error"))
    with pytest.raises(DecisionError) as info:
        _caller().generate("s", "c")
    assert info.value.retryable and len(bodies) == MAX_SERVER_ERROR_RETRIES + 1
    assert classify(info.value) == ("llm_error", True, None)


def test_network_errors_are_retried_then_reported(monkeypatch):
    calls = []

    def flaky(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, json=_body("fine"))

    assert _caller(transport=httpx.MockTransport(flaky)).generate("s", "c").text == "fine"
    assert len(calls) == 2

    def down(request):
        raise httpx.ReadTimeout("stalled", request=request)

    with pytest.raises(DecisionError) as info:
        _caller(transport=httpx.MockTransport(down)).generate("s", "c")
    assert info.value.retryable and "network error" in str(info.value)


def _h11_rejects_the_header(request):
    """What httpx raises for a key with trailing whitespace: h11 quotes the
    whole header value, key included."""
    raise httpx.LocalProtocolError(f"Illegal header value b'{request.headers['authorization']}'")


def test_an_unsendable_request_fails_at_once_without_leaking_the_key(_openai_env, caplog):
    sleeps = _openai_env
    caller = OpenAICaller(KEY + " ", "test-model", 1000, transport=httpx.MockTransport(_h11_rejects_the_header))
    with caplog.at_level(logging.DEBUG), pytest.raises(DecisionError) as info:
        judge_segments(_segments(), caller=caller)
    assert not info.value.retryable and classify(info.value) == ("llm_error", False, None)
    assert "OPENAI_API_KEY" in str(info.value) and "LocalProtocolError" in str(info.value)
    # pipeline logs the job failure with logger.exception: the traceback must be keyless too
    shown = "".join(traceback.format_exception(info.value)) + caplog.text
    assert "SECRET" not in shown and sleeps == []


def test_a_base_url_without_a_scheme_is_a_config_error(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "api.openai.com/v1")  # rejected before any connection is made
    get_settings.cache_clear()
    with pytest.raises(DecisionError) as info:
        make_caller().generate("s", "c")
    assert not info.value.retryable and "UnsupportedProtocol" in str(info.value)


def test_network_error_text_is_redacted_in_the_warnings_and_the_error(caplog):
    def down(request):
        raise httpx.ReadTimeout(f"stalled sending {request.headers['authorization']}", request=request)

    with caplog.at_level(logging.WARNING), pytest.raises(DecisionError) as info:
        _caller(transport=httpx.MockTransport(down)).generate("s", "c")
    warnings = [r.getMessage() for r in caplog.records if "retrying" in r.getMessage()]
    assert len(warnings) == MAX_SERVER_ERROR_RETRIES and info.value.retryable
    shown = "".join(traceback.format_exception(info.value)) + caplog.text
    assert "SECRET" not in shown and "sk-***" in str(info.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_errors_fail_at_once_without_leaking_the_key(monkeypatch, status):
    message = (f"Incorrect API key provided: sk-test-****CRET. You can find your API key at "
               f"https://platform.openai.com/account/api-keys. (sent {KEY})")
    bodies = _fake(monkeypatch, lambda body: _error(status, message, code="invalid_api_key"))
    with pytest.raises(DecisionError) as info:
        judge_segments(_segments(), caller=_caller())
    text = str(info.value)
    assert len(bodies) == 1 and not info.value.retryable
    assert classify(info.value) == ("llm_error", False, None)
    assert "Incorrect API key provided" in text and f"{status}" in text
    assert KEY not in text and "sk-test" not in text and "****CRET" not in text


def test_stage_deadline(monkeypatch):
    _fake(monkeypatch, lambda body: _resp(200, _body(_answer(body["input"]))))
    caller = _caller(max_wall_s=1)
    caller.deadline = 0.0  # already past
    with pytest.raises(PipelineError) as info:
        judge_segments(_segments(), caller=caller)
    assert info.value.code == "timeout" and info.value.retryable
    caller.start_stage("chapters", 300)  # a new stage gets a fresh budget
    assert len(judge_segments(_segments(), caller=caller)) == 2


# ---------------------------------------------------------------- provider choice


def test_make_caller_builds_the_configured_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "cheap-model")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "low")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1/")
    get_settings.cache_clear()
    caller = make_caller()
    assert isinstance(caller, OpenAICaller)
    assert caller.model == "cheap-model" and caller.reasoning_effort == "low"
    assert str(caller.client.base_url) == "https://proxy.example/v1/"
    assert caller.client.headers["authorization"] == f"Bearer {KEY}"
    assert caller.client.timeout.read == get_settings().openai_request_timeout_s

    monkeypatch.setenv("LLM_PROVIDER", "Gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()
    assert isinstance(make_caller(), GeminiCaller)


def test_defaults_use_the_budget_model_without_reasoning(monkeypatch):
    for name in ("LLM_PROVIDER", "OPENAI_MODEL", "OPENAI_REASONING_EFFORT", "OPENAI_RPM"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings(_env_file=None)  # the code defaults, whatever the local .env says
    assert defaults.llm_provider == "openai" and defaults.openai_model == "gpt-6-luna"
    assert defaults.openai_reasoning_effort == "none" and defaults.openai_rpm <= 500


def test_missing_openai_key_is_not_retryable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(DecisionError) as info:
        make_caller()
    assert str(info.value) == "OPENAI_API_KEY is not set" and not info.value.retryable


@pytest.mark.parametrize("pasted", [f"{KEY} ", f"{KEY}\n", f"\t{KEY}\r\n"])
def test_whitespace_around_the_key_is_stripped(monkeypatch, pasted):
    monkeypatch.setenv("OPENAI_API_KEY", pasted)  # what a quoted .env value or a copied env var keeps
    get_settings.cache_clear()
    assert make_caller().client.headers["authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize("pasted", ["“sk-test-0123456789SECRET”", "sk-test-0123 456789SECRET",
                                    "sk-test-0123456789SECRET\x7f", "   "])
def test_a_mangled_key_fails_the_job_at_once_by_name(monkeypatch, pasted):
    monkeypatch.setenv("OPENAI_API_KEY", pasted)
    get_settings.cache_clear()
    builds = []
    real = gemini_client.make_caller
    monkeypatch.setattr(gemini_client, "make_caller", lambda: builds.append(1) or real())
    bodies = _fake(monkeypatch, lambda body: _resp(200, _body(_answer(body["input"]))))
    with pytest.raises(DecisionError) as info:
        judge_segments(_segments(60), caller=LazyCaller())
    assert classify(info.value) == ("llm_error", False, None)
    assert "OPENAI_API_KEY" in str(info.value) and "SECRET" not in str(info.value)
    assert len(builds) == 1 and bodies == []  # not mistaken for a flaky answer, retried and split


def test_the_gemini_key_is_cleaned_the_same_way(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "“AIza-test-key”")
    get_settings.cache_clear()
    with pytest.raises(DecisionError) as info:
        make_caller()
    assert not info.value.retryable and "GEMINI_API_KEY" in str(info.value)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key \n")
    get_settings.cache_clear()
    assert isinstance(make_caller(), GeminiCaller)


def test_unknown_provider_is_not_retryable(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    get_settings.cache_clear()
    with pytest.raises(DecisionError) as info:
        make_caller()
    assert not info.value.retryable and "LLM_PROVIDER" in str(info.value)
    assert LazyCaller().model == ""


def test_lazy_caller_model_follows_the_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "o-model")
    monkeypatch.setenv("GEMINI_MODEL", "g-model")
    get_settings.cache_clear()
    lazy = LazyCaller()
    assert lazy.model == "o-model" and lazy._real is None  # no client until the first call
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    get_settings.cache_clear()
    assert LazyCaller().model == "g-model"


def test_lazy_caller_uses_openai_and_shares_its_usage(monkeypatch):
    _fake(monkeypatch, lambda body: _resp(200, _body(_answer(body["input"]))))
    lazy = LazyCaller()
    judge_segments(_segments(3), caller=lazy)
    assert isinstance(lazy._real, OpenAICaller) and lazy.usage.calls == 1 and lazy.usage.prompt_tokens == 100


def test_switching_model_invalidates_saved_decisions(monkeypatch, tmp_path):
    bodies = _fake(monkeypatch, lambda body: _resp(200, _body(_answer(body["input"]))))
    path = tmp_path / "decisions.json"
    judge_segments(_segments(3), caller=LazyCaller(), persist_path=path)
    judge_segments(_segments(3), caller=LazyCaller(), persist_path=path)
    assert len(bodies) == 1  # same model: reused
    monkeypatch.setenv("OPENAI_MODEL", "another-model")
    get_settings.cache_clear()
    judge_segments(_segments(3), caller=LazyCaller(), persist_path=path)
    assert len(bodies) == 2 and bodies[1]["model"] == "another-model"
    assert json.loads(path.read_text())["model"] == "another-model"


def test_gemini_style_responses_still_report_their_finish_reason():
    gemini_response = SimpleNamespace(candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")])
    assert gemini_client._finish_reason(gemini_response) == "MAX_TOKENS"
    assert gemini_client._finish_reason(openai_client.OpenAIResponse("", "MAX_TOKENS")) == "MAX_TOKENS"
