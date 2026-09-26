import sys

import pytest

from core.proc import STDERR_TAIL_BYTES, MediaCommandError, run_checked, set_poll_hook


def test_failure_message_carries_command_and_stderr_tail():
    script = "import sys; sys.stderr.write('x' * 5000 + 'REAL_REASON'); sys.exit(3)"
    with pytest.raises(MediaCommandError) as info:
        run_checked([sys.executable, "-c", script], timeout=30)
    err = info.value
    assert err.returncode == 3
    assert "REAL_REASON" in str(err)
    assert "exit 3" in str(err)
    assert "-c" in str(err)  # the command line is in the message
    # only the tail is kept, not the whole 5 kB
    assert len(err.stderr_tail) <= STDERR_TAIL_BYTES + 1


def test_timeout_is_reported_as_media_command_error():
    with pytest.raises(MediaCommandError) as info:
        run_checked([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.5)
    assert "timed out" in str(info.value)


def test_success_returns_stdout():
    result = run_checked([sys.executable, "-c", "print('hello')"], timeout=30)
    assert result.stdout.strip() == "hello"


def test_poll_hook_runs_and_can_abort():
    calls = []

    class Abort(Exception):
        pass

    def hook():
        calls.append(1)
        raise Abort

    set_poll_hook(hook)
    try:
        with pytest.raises(Abort):
            run_checked([sys.executable, "-c", "import time; time.sleep(10)"], timeout=30, poll_interval=0.2)
    finally:
        set_poll_hook(None)
    assert calls


def test_poll_hook_path_still_reports_failures():
    set_poll_hook(lambda: None)
    try:
        with pytest.raises(MediaCommandError) as info:
            run_checked(
                [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"],
                timeout=30, poll_interval=0.2,
            )
    finally:
        set_poll_hook(None)
    assert "boom" in str(info.value)


def test_stream_checked_yields_stdout_in_chunks():
    from core.proc import stream_checked

    script = "import sys; sys.stdout.buffer.write(bytes(range(256)) * 10)"
    chunks = list(stream_checked([sys.executable, "-c", script], 1000, timeout=30))
    assert [len(c) for c in chunks] == [1000, 1000, 560]
    assert b"".join(chunks) == bytes(range(256)) * 10


def test_stream_checked_reports_failures_and_timeouts_like_run_checked():
    from core.proc import stream_checked

    script = "import sys; sys.stdout.write('x'); sys.stderr.write('REAL_REASON'); sys.exit(3)"
    with pytest.raises(MediaCommandError) as info:
        list(stream_checked([sys.executable, "-c", script], 1000, timeout=30))
    assert info.value.returncode == 3 and "REAL_REASON" in str(info.value)
    with pytest.raises(MediaCommandError) as info:
        list(stream_checked([sys.executable, "-c", "import time; time.sleep(10)"], 1000, timeout=0.5))
    assert "timed out" in str(info.value)


def test_stream_checked_runs_the_poll_hook_which_can_abort():
    from core.proc import stream_checked

    class Abort(Exception):
        pass

    def hook():
        raise Abort

    script = "import sys, time\nfor _ in range(50):\n    sys.stdout.write('x' * 10); sys.stdout.flush(); time.sleep(0.05)"
    set_poll_hook(hook)
    try:
        with pytest.raises(Abort):
            list(stream_checked([sys.executable, "-c", script], 10, timeout=30, poll_interval=0.1))
    finally:
        set_poll_hook(None)
