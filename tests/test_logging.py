"""Log lines carry the request id; LOG_JSON=true writes one JSON object per line."""

import json
import logging

from fastapi.testclient import TestClient

from core.logging import JsonFormatter, configure_logging, request_id_var


def _record(message: str) -> logging.LogRecord:
    record = logging.LogRecord("hc.test", logging.WARNING, __file__, 1, message, None, None)
    record.request_id = request_id_var.get()
    return record


def test_json_lines_are_parseable_and_carry_the_request_id():
    token = request_id_var.set("abc123")
    try:
        line = JsonFormatter().format(_record("disk is low"))
    finally:
        request_id_var.reset(token)
    entry = json.loads(line)
    assert entry["msg"] == "disk is low" and entry["level"] == "WARNING" and entry["request_id"] == "abc123"


def test_outside_a_request_the_id_is_a_dash():
    assert json.loads(JsonFormatter().format(_record("startup")))["request_id"] == "-"


def test_request_ids_are_echoed_and_do_not_leak_past_the_request(capsys):
    from api.main import app

    configure_logging(json_lines=True)
    try:
        response = TestClient(app).get("/no-such-api-path-for-logging", headers={"X-Request-ID": "req-4242-test"})
        assert response.headers["x-request-id"] == "req-4242-test"
        logging.getLogger("hc.test").warning("inside the test")  # outside the request again
    finally:
        configure_logging(json_lines=False)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert lines and lines[-1]["request_id"] == "-"
