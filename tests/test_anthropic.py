import json
import logging
import re

import anthropic
import httpx2
import pytest

from core.config import Settings, get_settings
from core.errors import PipelineError, classify
from core.models import Segment
from decide import anthropic_client
from decide.anthropic_client import AnthropicCaller, request_params
from decide.gemini_client import (
    DEFAULT_429_WAIT_S,
    MAX_429_WAIT_S,
    MAX_RATE_LIMIT_RETRIES,
    MAX_SERVER_ERROR_RETRIES,
    DecisionError,
    LazyCaller,
    judge_segments,
    make_caller,
)

KEY = "sk-ant-test-0123456789abcdefSECRET"


@pytest.fixture(autouse=True)
def _anthropic_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    for name in ("ANTHROPIC_MODEL", "ANTHROPIC_RPM", "ANTHROPIC_MAX_OUTPUT_TOKENS", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    sleeps = []
    monkeypatch.setattr(anthropic_client.time, "sleep", lambda s: sleeps.append(s))
    yield sleeps
    get_settings.cache_clear()


def _message(text="", stop_reason="end_turn", usage=None, content=None):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": content if content is not None else [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 0,
                           "cache_read_input_tokens": 0},
    }


def _error(status, kind, message, headers=None):
    return httpx2.Response(status, json={"type": "error", "error": {"type": kind, "message": message},
                                         "request_id": "req_1"}, headers=headers)


def _caller(handler, **kw):
    """A real SDK client whose HTTP goes to `handler(request) -> httpx2.Response`
    (or raises); returns (caller, recorded requests)."""
    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    http = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(record))
    return AnthropicCaller(KEY, "claude-haiku-4-5", 1000, http_client=http, **kw), requests


def _body(request) -> dict:
    return json.loads(request.content)


def _segments(n=2):
    return [Segment(speaker="A", start=float(i), end=i + 1.0, text=f"sentence {i}") for i in range(n)]


def _judged(contents: str) -> list[int]:
    section = contents.split("JUDGE:", 1)[1].split("CONTEXT", 1)[0]
    return [int(n) for n in re.findall(r"^\[(\d+)\]", section, re.MULTILINE)]


def _answer(request) -> str:
    contents = _body(request)["messages"][0]["content"]
    return "\n".join(f"{n} k 4 - idea" for n in _judged(contents))


# ---------------------------------------------------------------- request / response


def test_request_shape_headers_and_endpoint():
    caller, requests = _caller(lambda r: httpx2.Response(200, json=_message("ok")))
    caller.generate("SYSTEM RULES", "the transcript")
    (request,) = requests
    assert request.url.path == "/v1/messages" and request.headers["x-api-key"] == KEY
    assert request.headers.get("anthropic-version")
    body = _body(request)
    assert body["model"] == "claude-haiku-4-5" and body["max_tokens"] == 16000
    assert body["system"] == [{"type": "text", "text": "SYSTEM RULES", "cache_control": {"type": "ephemeral"}}]
    assert body["messages"] == [{"role": "user", "content": "the transcript"}]
    assert "thinking" not in body and "output_config" not in body
    assert caller.client.max_retries == 0  # this caller owns retrying


def test_a_schema_asks_for_json():
    params = request_params("m", "s", "c", {"type": "object"}, max_output_tokens=10)
    assert params["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}}


def test_text_and_usage_are_parsed():
    usage = {"input_tokens": 70, "output_tokens": 40, "cache_creation_input_tokens": 20,
             "cache_read_input_tokens": 10}
    content = [{"type": "text", "text": "part one, "}, {"type": "text", "text": "part two"}]
    caller, _ = _caller(lambda r: httpx2.Response(200, json=_message(content=content, usage=usage)))
    response = caller.generate("s", "c")
    assert response.text == "part one, part two" and response.finish_reason == "end_turn"
    assert caller.usage.calls == 1 and caller.usage.output_tokens == 40
    assert caller.usage.prompt_tokens == 100  # cache writes and reads are input too


def test_judging_runs_unchanged_on_the_claude_caller():
    caller, requests = _caller(lambda r: httpx2.Response(200, json=_message(_answer(r))))
    judgments = judge_segments(_segments(5), caller=caller)
    assert [j.index for j in judgments] == list(range(5))
    assert all(j.decision == "keep" and j.highlight_category == "concept" for j in judgments)
    assert len(requests) == 1 and "LEARN" in _body(requests[0])["system"][0]["text"]


def test_a_truncated_answer_splits_the_chunk(caplog):
    sizes = []

    def handler(request):
        n = len(_judged(_body(request)["messages"][0]["content"]))
        sizes.append(n)
        if n > 2:
            return httpx2.Response(200, json=_message("0 k 4 - -\n1 k", stop_reason="max_tokens"))
        return httpx2.Response(200, json=_message(_answer(request)))

    caller, _ = _caller(handler)
    with caplog.at_level(logging.WARNING):
        judgments = judge_segments(_segments(8), caller=caller)
    assert [j.index for j in judgments] == list(range(8))
    assert sizes.count(8) == 1 and sizes.count(2) >= 1  # split at once, no same-size retry
    assert any("MAX_TOKENS" in r.message for r in caplog.records)


def test_a_refusal_is_an_invalid_answer_and_is_retried():
    answers = []

    def handler(request):
        answers.append(request)
        if len(answers) == 1:
            return httpx2.Response(200, json=_message(content=[], stop_reason="refusal"))
        return httpx2.Response(200, json=_message(_answer(request)))

    caller, _ = _caller(handler)
    assert [j.index for j in judge_segments(_segments(3), caller=caller)] == [0, 1, 2]
    assert len(answers) == 2


# ---------------------------------------------------------------- errors


@pytest.mark.parametrize("status,kind,message", [
    (402, "billing_error", "Your account has a billing problem."),
    (400, "invalid_request_error",
     "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing."),
])
def test_no_credit_fails_fast_and_is_not_retryable(status, kind, message):
    caller, requests = _caller(lambda r: _error(status, kind, message))
    with pytest.raises(PipelineError) as info:
        caller.generate("s", "c")
    assert classify(info.value) == ("llm_quota_exhausted", False, None)
    assert "console.anthropic.com" in str(info.value) and len(requests) == 1


@pytest.mark.parametrize("headers,expected", [
    ({"retry-after": "7"}, 7.0),
    ({"retry-after-ms": "2500"}, 2.5),
    ({"retry-after": "0"}, 1.0),  # never hammer: at least a second
    ({"retry-after": "3600"}, MAX_429_WAIT_S),
    ({}, DEFAULT_429_WAIT_S),
])
def test_rate_limit_waits_for_what_the_server_asks(_anthropic_env, headers, expected):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return _error(429, "rate_limit_error", "Number of requests has exceeded your rate limit", headers)
        return httpx2.Response(200, json=_message("ok"))

    caller, _ = _caller(handler)
    assert caller.generate("s", "c").text == "ok"
    assert _anthropic_env == [expected] and caller.usage.retries == 1


def test_endless_rate_limiting_gives_up_retryably():
    caller, requests = _caller(lambda r: _error(429, "rate_limit_error", "slow down", {"retry-after": "1"}))
    with pytest.raises(DecisionError) as info:
        caller.generate("s", "c")
    assert info.value.retryable and len(requests) == MAX_RATE_LIMIT_RETRIES + 1


def test_overloaded_and_server_errors_are_retried_with_backoff(_anthropic_env):
    answers = iter([_error(529, "overloaded_error", "Overloaded"), _error(500, "api_error", "Internal"),
                    httpx2.Response(200, json=_message("ok"))])
    caller, _ = _caller(lambda r: next(answers))
    assert caller.generate("s", "c").text == "ok"
    assert _anthropic_env == [5.0, 10.0]


def test_a_persistent_overload_is_a_retryable_decision_error():
    caller, requests = _caller(lambda r: _error(529, "overloaded_error", "Overloaded"))
    with pytest.raises(DecisionError) as info:
        caller.generate("s", "c")
    assert info.value.retryable and "529" in str(info.value)
    assert len(requests) == MAX_SERVER_ERROR_RETRIES + 1


def test_network_errors_are_retried_then_reported(_anthropic_env):
    def handler(request):
        raise httpx2.ConnectError("connection refused", request=request)

    caller, requests = _caller(handler)
    with pytest.raises(DecisionError) as info:
        caller.generate("s", "c")
    assert info.value.retryable and len(requests) == MAX_SERVER_ERROR_RETRIES + 1
    assert _anthropic_env == [5.0, 10.0, 15.0]


@pytest.mark.parametrize("status,kind", [
    (400, "invalid_request_error"), (401, "authentication_error"), (403, "permission_error"),
    (404, "not_found_error"), (413, "request_too_large"),
])
def test_permanent_errors_fail_at_once_without_leaking_the_key(status, kind, caplog):
    caller, requests = _caller(lambda r: _error(status, kind, f"bad request with key {KEY}"))
    with caplog.at_level(logging.WARNING), pytest.raises(DecisionError) as info:
        caller.generate("s", "c")
    assert not info.value.retryable and len(requests) == 1
    assert str(status) in str(info.value) and KEY not in str(info.value) and KEY not in caplog.text
    assert info.value.__cause__ is None  # the SDK error (with the raw body) is not chained into job.log


def test_stage_deadline():
    caller, requests = _caller(lambda r: httpx2.Response(200, json=_message("ok")), max_wall_s=0.0001)
    caller.deadline = 0.0
    with pytest.raises(PipelineError) as info:
        caller.generate("s", "c")
    assert classify(info.value)[:2] == ("timeout", True) and not requests


# ---------------------------------------------------------------- settings / provider switch


def test_defaults_use_claude_haiku(monkeypatch):
    for name in ("LLM_PROVIDER", "ANTHROPIC_MODEL", "ANTHROPIC_RPM"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings(_env_file=None)  # the code defaults, whatever the local .env says
    assert defaults.llm_provider == "anthropic" and defaults.anthropic_model == "claude-haiku-4-5"
    assert defaults.anthropic_rpm <= 50


def test_make_caller_builds_the_claude_caller():
    caller = make_caller()
    assert isinstance(caller, AnthropicCaller) and caller.model == "claude-haiku-4-5"
    assert caller.client.max_retries == 0


def test_missing_claude_key_is_not_retryable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(DecisionError) as info:
        make_caller()
    assert not info.value.retryable and "ANTHROPIC_API_KEY is not set" in str(info.value)


def test_a_pasted_key_with_spaces_or_quotes_is_cleaned_or_refused(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", f"  {KEY}\n")
    get_settings.cache_clear()
    assert make_caller()._api_key == KEY
    monkeypatch.setenv("ANTHROPIC_API_KEY", f"“{KEY}”")
    get_settings.cache_clear()
    with pytest.raises(DecisionError) as info:
        make_caller()
    assert not info.value.retryable and "ANTHROPIC_API_KEY" in str(info.value) and KEY not in str(info.value)


def test_lazy_caller_model_follows_the_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "c-model")
    monkeypatch.setenv("OPENAI_MODEL", "o-model")
    get_settings.cache_clear()
    assert LazyCaller().model == "c-model"
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    assert LazyCaller().model == "o-model"
