"""ingest/zoom.py against a mocked Zoom (httpx.MockTransport): no real calls."""

import base64
import logging
import shutil
import subprocess
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from core.config import get_settings
from core.errors import PipelineError
from ingest import zoom

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100
# Resolved at collection: a test that points FFMPEG_BIN at a stub can leave
# the stub's folder on PATH for the rest of the run.
FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
TOKEN_URL = "https://zoom.us/oauth/token"


@pytest.fixture
def zoom_env(monkeypatch):
    monkeypatch.setenv("ZOOM_ACCOUNT_ID", "acct-1")
    monkeypatch.setenv("ZOOM_CLIENT_ID", "client-1")
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "secret-1")
    monkeypatch.setenv("ZOOM_HOST_EMAIL", "host@example.com")
    get_settings.cache_clear()


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeZoom:
    """Answers the token endpoint itself and hands every other request to
    `routes(request)`; records everything it saw."""

    def __init__(self, routes=None, token_status=200, token_failures=()):
        self.routes = routes or (lambda request: httpx.Response(404, json={"code": 3301, "message": "nope"}))
        self.token_status = token_status
        # answered (a Response) or raised (an exception) by the token
        # endpoint, in turn, before it starts minting
        self.token_failures = list(token_failures)
        self.minted = 0
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            if self.token_failures:
                failure = self.token_failures.pop(0)
                if isinstance(failure, Exception):
                    raise failure
                return failure
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"reason": "Invalid client_id or client_secret",
                                                               "error": "invalid_client"})
            self.minted += 1
            return httpx.Response(200, json={"access_token": f"tok{self.minted}", "expires_in": 3600,
                                             "scope": " ".join(zoom.REQUIRED_SCOPES)})
        return self.routes(request)

    def api_requests(self):
        return [r for r in self.requests if str(r.url) != TOKEN_URL]


def _client(fake, clock=None):
    clock = clock or FakeClock()
    return zoom.ZoomClient(transport=httpx.MockTransport(fake), sleep=clock.sleep, clock=clock)


def _file(ftype, rtype, start, end, url, size=None, status="completed"):
    return {"id": url, "file_type": ftype, "recording_type": rtype, "recording_start": start, "recording_end": end,
            "download_url": url, "file_size": size, "status": status, "file_extension": ftype}


def _meeting(files, uuid="abc==", start="2026-09-20T10:00:00Z", **extra):
    return {"uuid": uuid, "id": 123, "topic": "Weekly sync", "start_time": start, "duration": 60,
            "total_size": 1, "host_email": "host@example.com", "recording_files": files, **extra}


# ---------------------------------------------------------------- auth


def test_missing_credentials_are_a_clear_not_configured_error():
    with pytest.raises(zoom.ZoomNotConfigured) as err:
        zoom.ZoomClient(transport=httpx.MockTransport(FakeZoom()))
    assert err.value.code == "zoom_auth" and not err.value.retryable
    assert "ZOOM_ACCOUNT_ID" in str(err.value) and "ZOOM_CLIENT_SECRET" in str(err.value)
    assert zoom.is_configured() is False


def test_token_is_minted_with_basic_auth_cached_and_reminted_near_expiry(zoom_env):
    fake = FakeZoom(lambda r: httpx.Response(200, json={"uuid": "abc==", "recording_files": []}))
    clock = FakeClock()
    client = _client(fake, clock)
    client.get_meeting("abc==")
    client.get_meeting("abc==")
    assert fake.minted == 1  # cached

    token_request = fake.requests[0]
    assert token_request.method == "POST"
    assert token_request.headers["authorization"] == "Basic " + base64.b64encode(b"client-1:secret-1").decode()
    assert parse_qs(token_request.content.decode()) == {"grant_type": ["account_credentials"],
                                                        "account_id": ["acct-1"]}
    assert all(r.headers["authorization"] == "Bearer tok1" for r in fake.api_requests())

    clock.t += 3600 - zoom.TOKEN_EARLY_REFRESH_S + 1  # a few minutes before Zoom's expiry
    client.get_meeting("abc==")
    assert fake.minted == 2 and fake.api_requests()[-1].headers["authorization"] == "Bearer tok2"


def test_a_401_remints_the_token_once(zoom_env):
    answers = iter([httpx.Response(401, json={"code": 124, "message": "Invalid access token."}),
                    httpx.Response(200, json={"uuid": "abc==", "recording_files": []})])
    fake = FakeZoom(lambda r: next(answers))
    assert _client(fake).get_meeting("abc==")["uuid"] == "abc=="
    assert fake.minted == 2
    assert [r.headers["authorization"] for r in fake.api_requests()] == ["Bearer tok1", "Bearer tok2"]


def test_a_rejected_token_request_is_zoom_auth_and_names_the_scopes(zoom_env):
    fake = FakeZoom(token_status=400)
    with pytest.raises(PipelineError) as err:
        _client(fake).get_meeting("abc==")
    assert err.value.code == "zoom_auth" and not err.value.retryable
    assert "ACTIVATED" in str(err.value) and zoom.REQUIRED_SCOPES[0] in str(err.value)
    assert "secret-1" not in str(err.value)
    assert len(fake.requests) == 1  # a refusal is not retried


def test_a_token_endpoint_outage_is_waited_out(zoom_env):
    fake = FakeZoom(lambda r: httpx.Response(200, json={"uuid": "abc==", "recording_files": []}),
                    token_failures=[httpx.Response(503, text="Service Unavailable"),
                                    httpx.ConnectError("connection reset"),
                                    httpx.Response(429, headers={"Retry-After": "7"})])
    clock = FakeClock()
    assert _client(fake, clock).get_meeting("abc==")["uuid"] == "abc=="
    assert fake.minted == 1 and clock.t == 1000.0 + 2 + 4 + 7  # two back-offs, then Retry-After


@pytest.mark.parametrize(("status", "headers", "retry_after"), [
    (503, {}, zoom.UNAVAILABLE_RETRY_AFTER_S),
    (429, {"Retry-After": "5"}, 5),
])
def test_a_token_endpoint_that_stays_down_is_retryable_not_zoom_auth(zoom_env, status, headers, retry_after):
    # A Zoom outage must not look like a broken app: zoom_auth is not retried
    # by n8n and tells the owner to fix the Marketplace app.
    fake = FakeZoom(token_failures=[httpx.Response(status, headers=headers, text="busy") for _ in range(10)])
    with pytest.raises(PipelineError) as err:
        _client(fake).get_meeting("abc==")
    assert err.value.code == "zoom_unavailable" and err.value.retryable
    assert err.value.retry_after_s == retry_after
    assert "ACTIVATED" not in str(err.value) and f"HTTP {status}" in str(err.value)
    attempts = zoom.RATE_LIMIT_ATTEMPTS if status == 429 else zoom.TRANSIENT_ATTEMPTS
    assert len(fake.requests) == 1 + attempts and fake.api_requests() == []


def test_an_api_outage_is_retryable_zoom_unavailable(zoom_env):
    fake = FakeZoom(lambda r: httpx.Response(502, text="Bad Gateway"))
    with pytest.raises(PipelineError) as err:
        _client(fake).get_meeting("abc==")
    assert err.value.code == "zoom_unavailable" and err.value.retryable
    assert err.value.retry_after_s == zoom.UNAVAILABLE_RETRY_AFTER_S
    assert len(fake.api_requests()) == 1 + zoom.TRANSIENT_ATTEMPTS


def test_missing_scopes_and_unknown_meetings_map_to_their_codes(zoom_env):
    scope = FakeZoom(lambda r: httpx.Response(400, json={"code": 4711, "message": "Invalid access token, does "
                                                                                  "not contain scopes"}))
    with pytest.raises(PipelineError) as err:
        _client(scope).get_meeting("abc==")
    assert err.value.code == "zoom_auth"
    with pytest.raises(PipelineError) as err:
        _client(FakeZoom()).get_meeting("abc==")
    assert err.value.code == "zoom_not_found"


def test_rate_limit_waits_retry_after_then_succeeds(zoom_env):
    answers = iter([httpx.Response(429, headers={"Retry-After": "7"}),
                    httpx.Response(200, json={"uuid": "abc==", "recording_files": []})])
    clock = FakeClock()
    _client(FakeZoom(lambda r: next(answers)), clock).get_meeting("abc==")
    assert clock.t == 1007.0


# ---------------------------------------------------------------- listing


def test_list_pages_and_splits_a_long_range_into_month_windows(zoom_env):
    def routes(request):
        params = dict(request.url.params)
        assert request.url.path == "/v2/users/host@example.com/recordings"
        assert params["page_size"] == "300"
        start = params["from"]
        if start == "2026-06-01" and "next_page_token" not in params:
            return httpx.Response(200, json={"meetings": [_meeting([], uuid="u1", start="2026-06-02T10:00:00Z")],
                                             "next_page_token": "p2"})
        if start == "2026-06-01":
            assert params["next_page_token"] == "p2"
            return httpx.Response(200, json={"meetings": [_meeting([], uuid="u2", start="2026-06-20T10:00:00Z")],
                                             "next_page_token": ""})
        return httpx.Response(200, json={"meetings": [_meeting([], uuid=f"w{start}", start=f"{start}T09:00:00Z")]})

    fake = FakeZoom(routes)
    start, end = date(2026, 6, 1), date(2026, 6, 1) + timedelta(days=69)  # 70 days
    summaries = _client(fake).list_recordings("host@example.com", start, end)

    windows = sorted({(r.url.params["from"], r.url.params["to"]) for r in fake.api_requests()})
    assert len(windows) == 3
    for lo, hi in windows:
        assert (date.fromisoformat(hi) - date.fromisoformat(lo)).days + 1 <= 30
    assert windows[0][0] == "2026-06-01" and windows[-1][1] == end.isoformat()
    assert len(fake.api_requests()) == 4  # the first window had two pages
    starts = [s["start_time"] for s in summaries]
    assert starts == sorted(starts, reverse=True) and len(summaries) == 4
    assert {"uuid", "topic", "parts", "has_transcript", "recording_ready", "status"} <= set(summaries[0])


def test_date_windows_cover_the_range_exactly():
    windows = zoom.date_windows(date(2026, 1, 1), date(2026, 3, 11))
    assert windows[0] == (date(2026, 1, 1), date(2026, 1, 30))
    assert windows[-1][1] == date(2026, 3, 11)
    assert all((b - a).days < 30 for a, b in windows)
    assert all(windows[i][1] + timedelta(days=1) == windows[i + 1][0] for i in range(len(windows) - 1))


def test_uuid_single_vs_double_encoding(zoom_env):
    assert zoom.encode_uuid("4444AAAiAAAAAiAiAiiAii==") == "4444AAAiAAAAAiAiAiiAii%3D%3D"
    assert zoom.encode_uuid("ab+c/d==") == "ab%2Bc%2Fd%3D%3D"  # a lone "/" inside: once
    assert zoom.encode_uuid("/ajXp112QmuoKj4854875==") == "%252FajXp112QmuoKj4854875%253D%253D"
    assert zoom.encode_uuid("ab//c==") == "ab%252F%252Fc%253D%253D"

    fake = FakeZoom(lambda r: httpx.Response(200, json={"uuid": "x", "recording_files": []}))
    _client(fake).get_meeting("/ajXp112QmuoKj4854875==")
    assert fake.api_requests()[0].url.raw_path == b"/v2/meetings/%252FajXp112QmuoKj4854875%253D%253D/recordings"


# ---------------------------------------------------------------- choosing files


def test_mp4_priority_ignores_cc_variants_and_non_mp4():
    files = [
        _file("MP4", "gallery_view", "2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z", "https://zoom.us/g"),
        _file("MP4", "shared_screen_with_speaker_view(CC)", "2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z",
              "https://zoom.us/cc"),
        _file("M4A", "audio_only", "2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z", "https://zoom.us/a"),
        _file("MP4", "active_speaker", "2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z", "https://zoom.us/s"),
        _file("CHAT", "chat_file", "2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z", "https://zoom.us/c"),
    ]
    choice = zoom.choose_files(_meeting(files))
    assert choice.video_type == "active_speaker"
    assert [p.video["download_url"] for p in choice.parts] == ["https://zoom.us/s"]
    assert choice.verdict == "transcript_not_ready"


def test_two_part_meeting_pairs_transcripts_by_time_when_the_longer_part_is_second():
    # host stopped the recording after 5 minutes and restarted it for 50
    short = ("2026-09-20T10:00:00Z", "2026-09-20T10:05:00Z")
    long_ = ("2026-09-20T10:10:00Z", "2026-09-20T11:00:00Z")
    files = [
        _file("MP4", "shared_screen_with_speaker_view", *long_, "https://zoom.us/v2"),
        _file("TRANSCRIPT", "audio_transcript", *long_, "https://zoom.us/t2"),
        _file("TRANSCRIPT", "audio_transcript", *short, "https://zoom.us/t1"),
        _file("MP4", "shared_screen_with_speaker_view", *short, "https://zoom.us/v1"),
        _file("MP4", "gallery_view", *long_, "https://zoom.us/g2"),
    ]
    choice = zoom.choose_files(_meeting(files))
    assert [(p.video["download_url"], p.transcript["download_url"]) for p in choice.parts] == [
        ("https://zoom.us/v1", "https://zoom.us/t1"), ("https://zoom.us/v2", "https://zoom.us/t2")]
    assert choice.transcript_kind == "TRANSCRIPT" and choice.verdict is None


FIRST = ("2026-09-20T10:00:00Z", "2026-09-20T10:30:00Z")
SECOND = ("2026-09-20T10:40:00Z", "2026-09-20T11:30:00Z")


def test_each_segment_gets_its_own_best_view_so_none_is_dropped():
    # the screen was shared only in the first segment, and Zoom names each
    # segment's files by what happened in it
    files = [
        _file("MP4", "active_speaker", *SECOND, "https://zoom.us/as2"),
        _file("TRANSCRIPT", "audio_transcript", *SECOND, "https://zoom.us/t2"),
        _file("MP4", "shared_screen_with_speaker_view", *FIRST, "https://zoom.us/ss1"),
        _file("MP4", "active_speaker", *FIRST, "https://zoom.us/as1"),
        _file("TRANSCRIPT", "audio_transcript", *FIRST, "https://zoom.us/t1"),
    ]
    meeting = _meeting(files, timezone="Asia/Karachi")
    choice = zoom.choose_files(meeting)
    assert [(p.video["download_url"], p.transcript["download_url"]) for p in choice.parts] == [
        ("https://zoom.us/ss1", "https://zoom.us/t1"), ("https://zoom.us/as2", "https://zoom.us/t2")]
    assert choice.verdict is None and zoom.summarize(meeting)["parts"] == 2
    info = zoom.meeting_info(meeting, choice)
    # kept so the final video's name gets the meeting's own day (core/naming.py)
    assert info["timezone"] == "Asia/Karachi"
    assert info["recording_type"] == "shared_screen_with_speaker_view+active_speaker"
    assert [p["recording_type"] for p in info["parts"]] == ["shared_screen_with_speaker_view", "active_speaker"]
    assert info["segments_without_video"] == 0


def test_segments_are_told_apart_despite_clock_jitter():
    files = [
        _file("MP4", "active_speaker", "2026-09-20T10:00:00Z", "2026-09-20T10:30:02Z", "https://zoom.us/as1"),
        _file("MP4", "gallery_view", "2026-09-20T10:00:01Z", "2026-09-20T10:30:01Z", "https://zoom.us/g1"),
        # restarted at once: its stamps overlap the first segment by 2 s
        _file("MP4", "gallery_view", "2026-09-20T10:30:00Z", "2026-09-20T10:45:00Z", "https://zoom.us/g2"),
    ]
    assert [p.video["download_url"] for p in zoom.choose_files(_meeting(files)).parts] == [
        "https://zoom.us/as1", "https://zoom.us/g2"]


def test_a_lone_transcript_is_never_paired_with_another_segments_video():
    files = [
        _file("MP4", "shared_screen_with_speaker_view", *FIRST, "https://zoom.us/ss1"),
        _file("MP4", "active_speaker", *SECOND, "https://zoom.us/as2"),
        _file("TRANSCRIPT", "audio_transcript", *SECOND, "https://zoom.us/t2"),
    ]
    choice = zoom.choose_files(_meeting(files))
    assert [(p.video["download_url"], p.transcript and p.transcript["download_url"]) for p in choice.parts] == [
        ("https://zoom.us/ss1", None), ("https://zoom.us/as2", "https://zoom.us/t2")]
    assert choice.verdict == "transcript_not_ready"

    # one video: a transcript of another stretch of time is not its transcript ...
    elsewhere = zoom.choose_files(_meeting([_file("MP4", "active_speaker", *FIRST, "https://zoom.us/v"),
                                            _file("TRANSCRIPT", "audio_transcript", *SECOND, "https://zoom.us/t")]))
    assert elsewhere.parts[0].transcript is None
    # ... but with no timestamps to compare, a lone pair belongs together
    blind = zoom.choose_files(_meeting([_file("MP4", "active_speaker", None, None, "https://zoom.us/v"),
                                        _file("TRANSCRIPT", "audio_transcript", None, None, "https://zoom.us/t")]))
    assert blind.parts[0].transcript["download_url"] == "https://zoom.us/t" and blind.verdict is None


def test_a_segment_with_audio_but_no_video_is_counted_not_dropped_silently():
    files = [
        _file("MP4", "active_speaker", *FIRST, "https://zoom.us/v1"),
        _file("TRANSCRIPT", "audio_transcript", *FIRST, "https://zoom.us/t1"),
        _file("M4A", "audio_only", *FIRST, "https://zoom.us/a1"),
        # the second segment's MP4 was deleted in Zoom
        _file("M4A", "audio_only", *SECOND, "https://zoom.us/a2"),
        _file("TRANSCRIPT", "audio_transcript", *SECOND, "https://zoom.us/t2"),
    ]
    meeting = _meeting(files)
    choice = zoom.choose_files(meeting)
    assert [p.video["download_url"] for p in choice.parts] == ["https://zoom.us/v1"]
    assert choice.segments_without_video == 1 and choice.verdict is None
    assert zoom.meeting_info(meeting, choice)["segments_without_video"] == 1


def test_closed_captions_only_when_there_is_no_audio_transcript():
    span = ("2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z")
    cc = _file("CC", "closed_caption", *span, "https://zoom.us/cc")
    video = _file("MP4", "active_speaker", *span, "https://zoom.us/v")
    transcript = _file("TRANSCRIPT", "audio_transcript", *span, "https://zoom.us/t")
    assert zoom.choose_files(_meeting([video, cc])).parts[0].transcript is cc
    both = zoom.choose_files(_meeting([video, cc, transcript]))
    assert both.parts[0].transcript is transcript and both.transcript_kind == "TRANSCRIPT"


def test_readiness_verdicts():
    span = ("2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z")
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    video = _file("MP4", "active_speaker", *span, "https://zoom.us/v")
    transcript = _file("TRANSCRIPT", "audio_transcript", *span, "https://zoom.us/t")

    assert zoom.readiness(_meeting([video, transcript]), now=now) == "ready"
    assert zoom.readiness(_meeting([{**video, "status": "processing"}, transcript]), now=now) == "recording_not_ready"
    assert zoom.readiness(_meeting([video]), now=now) == "transcript_not_ready"
    assert zoom.readiness(_meeting([video, {**transcript, "status": "processing"}]), now=now) == \
        "transcript_not_ready"
    # a day later the transcript isn't coming
    assert zoom.readiness(_meeting([video]), now=now + timedelta(days=2)) == "no_transcript"
    assert zoom.readiness(_meeting([_file("M4A", "audio_only", *span, "https://zoom.us/a")]), now=now) == \
        "source_unsupported"
    # a second part without its transcript makes the whole meeting transcript-missing
    later = ("2026-09-20T11:10:00Z", "2026-09-20T11:30:00Z")
    part2 = _file("MP4", "active_speaker", *later, "https://zoom.us/v2")
    summary = zoom.summarize(_meeting([video, transcript, part2]), now=now)
    assert summary["parts"] == 2 and summary["has_transcript"] is False and summary["recording_ready"] is True
    assert summary["status"] == "transcript_not_ready"


def test_not_ready_errors_carry_retry_hints():
    err = zoom.not_ready_error("transcript_not_ready")
    assert err.code == "transcript_not_ready" and err.retryable and err.retry_after_s == 300
    gone = zoom.not_ready_error("no_transcript")
    assert gone.code == "transcript_not_ready" and not gone.retryable
    assert zoom.not_ready_error("source_unsupported").code == "source_unsupported"


def test_wait_for_transcript_polls_until_it_arrives(zoom_env):
    span = ("2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z")
    video = _file("MP4", "active_speaker", *span, "https://zoom.us/v")
    transcript = _file("TRANSCRIPT", "audio_transcript", *span, "https://zoom.us/t")
    answers = iter([_meeting([video, transcript])])
    fetches, checkpoints = [], []

    class Client:
        def get_meeting(self, uuid):
            fetches.append(uuid)
            return next(answers)

    clock = FakeClock()
    now = datetime.now(UTC).isoformat()  # recent: the transcript is still expected
    first = _meeting([{**video, "recording_end": now}])
    meeting = zoom.wait_for_transcript(Client(), "abc==", first, max_wait_s=600, poll_s=60,
                                       checkpoint=lambda: checkpoints.append(clock.t), sleep=clock.sleep, clock=clock)
    assert zoom.choose_files(meeting).has_transcript
    assert len(fetches) == 1 and clock.t == 1060.0
    assert len(checkpoints) >= 60 / zoom.CHECKPOINT_EVERY_S  # heartbeat/cancel every few seconds


# ---------------------------------------------------------------- downloads


def _download_client(routes):
    return _client(FakeZoom(routes))


def test_redirect_to_a_foreign_host_drops_the_token_but_zoom_hosts_keep_it(zoom_env, tmp_path):
    def routes(request):
        host = request.url.host
        if host == "us06web.zoom.us":
            return httpx.Response(302, headers={"Location": "https://ssrweb.zoom.us/file/1"})
        if host == "ssrweb.zoom.us":
            return httpx.Response(302, headers={"Location": "https://cdn.example.net/signed?sig=abc"})
        return httpx.Response(200, content=MP4)

    fake = FakeZoom(routes)
    path = _client(fake).download_file("https://us06web.zoom.us/rec/download/x", tmp_path / "v.mp4",
                                       size=len(MP4), expect_mp4=True)
    assert path.read_bytes() == MP4
    seen = {r.url.host: r.headers.get("authorization") for r in fake.api_requests()}
    assert seen["us06web.zoom.us"] == "Bearer tok1" and seen["ssrweb.zoom.us"] == "Bearer tok1"
    assert seen["cdn.example.net"] is None
    assert not any("tok1" in str(r.url) for r in fake.requests)  # never in a query string


def test_signed_redirect_urls_and_the_token_never_reach_the_log(zoom_env, tmp_path, caplog):
    # job.log is an INFO handler on the root logger, and httpx logs every
    # request URL at INFO: only zoom.py muting httpx keeps signed CDN URLs out
    caplog.set_level(logging.INFO)

    def routes(request):
        if request.url.host == "us06web.zoom.us":
            return httpx.Response(302, headers={"Location": "https://cdn.example.net/f?Signature=SIGNEDSECRET"})
        return httpx.Response(200, content=MP4)

    fake = FakeZoom(routes)
    _client(fake).download_file("https://us06web.zoom.us/rec/download/x", tmp_path / "v.mp4", size=len(MP4),
                                expect_mp4=True)
    assert fake.api_requests()[-1].url.host == "cdn.example.net"  # the redirect really was followed
    assert "SIGNEDSECRET" not in caplog.text and "cdn.example.net" not in caplog.text
    assert "tok1" not in caplog.text


def test_lookalike_and_plain_http_hosts_never_get_the_token(zoom_env):
    client = _client(FakeZoom())
    assert client._may_send_token("https://zoom.us/rec/1")
    assert client._may_send_token("https://eu01web.zoom.us/rec/1")
    assert not client._may_send_token("https://evilzoom.us/rec/1")
    assert not client._may_send_token("https://zoom.us.evil.com/rec/1")
    assert not client._may_send_token("http://zoom.us/rec/1")


def test_a_file_without_an_mp4_header_is_zoom_download_invalid(zoom_env, tmp_path):
    page = b"<html>please sign in</html>"
    client = _download_client(lambda r: httpx.Response(200, content=page))
    with pytest.raises(PipelineError) as err:
        client.download_file("https://zoom.us/rec/download/x", tmp_path / "v.mp4", size=len(page), expect_mp4=True)
    assert err.value.code == "zoom_download_invalid" and err.value.retryable
    assert not (tmp_path / "v.mp4").exists()


def test_a_size_mismatch_is_zoom_download_invalid(zoom_env, tmp_path):
    client = _download_client(lambda r: httpx.Response(200, content=MP4))
    with pytest.raises(PipelineError) as err:
        client.download_file("https://zoom.us/rec/download/x", tmp_path / "v.mp4", size=len(MP4) + 5,
                             expect_mp4=True)
    assert err.value.code == "zoom_download_invalid" and "Zoom said" in str(err.value)


class _DroppingStream(httpx.SyncByteStream):
    def __init__(self, first: bytes):
        self.first = first

    def __iter__(self):
        yield self.first
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")


def test_a_dropped_download_resumes_with_a_range_request(zoom_env, tmp_path):
    half = len(MP4) // 2
    ranges = []

    def routes(request):
        ranges.append(request.headers.get("range"))
        if len(ranges) == 1:
            return httpx.Response(200, stream=_DroppingStream(MP4[:half]))
        assert request.headers["range"] == f"bytes={half}-"
        return httpx.Response(206, content=MP4[half:])

    progress = []
    client = _download_client(routes)
    path = client.download_file("https://zoom.us/rec/download/x", tmp_path / "v.mp4", size=len(MP4),
                                expect_mp4=True, on_bytes=progress.append)
    assert path.read_bytes() == MP4
    assert ranges == [None, f"bytes={half}-"]
    assert progress[-1] == len(MP4)


def test_a_download_that_keeps_dropping_gives_up(zoom_env, tmp_path):
    client = _download_client(lambda r: httpx.Response(200, stream=_DroppingStream(b"")))
    with pytest.raises(PipelineError) as err:
        client.download_file("https://zoom.us/rec/download/x", tmp_path / "v.mp4", size=len(MP4))
    assert err.value.code == "zoom_download_invalid"


def test_shift_vtt_moves_every_timing_line():
    text = "﻿WEBVTT\r\n\r\n1\r\n00:00:01.500 --> 00:00:04.000\r\nJane Doe: Hello.\r\n\r\n2\r\n" \
           "01:02.5 --> 01:03.000 align:start\r\nBob: Hi.\r\n"
    shifted = zoom.shift_vtt(text, 300.25)
    assert "WEBVTT" not in shifted
    assert "00:05:01.750 --> 00:05:04.250" in shifted
    assert "00:06:02.750 --> 00:06:03.250 align:start" in shifted
    assert "Jane Doe: Hello." in shifted


def test_two_part_download_joins_parts_and_offsets_the_second_transcript(zoom_env, tmp_path, monkeypatch):
    short = ("2026-09-20T10:00:00Z", "2026-09-20T10:05:00Z")
    long_ = ("2026-09-20T10:10:00Z", "2026-09-20T11:00:00Z")
    vtt1 = b"WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.000\nJane Doe: Part one.\n"
    vtt2 = b"WEBVTT\n\n1\n00:00:02.000 --> 00:00:05.500\nBob Smith: Part two.\n"
    mp4_2 = MP4 + b"second"
    bodies = {"/v1": MP4, "/v2": mp4_2, "/t1": vtt1, "/t2": vtt2}
    files = [
        _file("MP4", "active_speaker", *long_, "https://zoom.us/v2", len(mp4_2)),
        _file("MP4", "active_speaker", *short, "https://zoom.us/v1", len(MP4)),
        _file("TRANSCRIPT", "audio_transcript", *short, "https://zoom.us/t1", len(vtt1)),
        _file("TRANSCRIPT", "audio_transcript", *long_, "https://zoom.us/t2", len(vtt2)),
    ]
    client = _download_client(lambda r: httpx.Response(200, content=bodies[r.url.path]))
    joined = []

    def fake_concat(parts, dest, work):
        joined.append([p.name for p in parts])
        dest.write_bytes(b"".join(p.read_bytes() for p in parts))

    monkeypatch.setattr(zoom, "concat_parts", fake_concat)
    measured = {"part_01.mp4": 300.5, "part_02.mp4": 3000.0}  # part 1 ran 0.5 s longer than its timestamps
    progress = []
    source, vtt, info = zoom.download_meeting(
        "abc==", tmp_path, client=client, meeting=_meeting(files),
        on_progress=lambda done, total: progress.append((done, total)), probe_duration=lambda p: measured[p.name],
    )
    assert joined == [["part_01.mp4", "part_02.mp4"]]
    assert source.read_bytes() == MP4 + mp4_2
    combined = vtt.read_text(encoding="utf-8")
    assert combined.startswith("WEBVTT\n\n")
    assert "00:00:01.000 --> 00:00:03.000\nJane Doe: Part one." in combined
    assert "00:05:02.500 --> 00:05:06.000\nBob Smith: Part two." in combined  # shifted by 300.5 s
    assert [p["offset_s"] for p in info["parts"]] == [0.0, 300.5]
    assert info["topic"] == "Weekly sync" and info["transcript"] == "TRANSCRIPT"
    assert progress[-1] == (sum(len(b) for b in bodies.values()),) * 2
    assert not (tmp_path / "tmp" / "zoom").exists()


def test_download_without_a_transcript_returns_only_the_video(zoom_env, tmp_path):
    span = ("2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z")
    client = _download_client(lambda r: httpx.Response(200, content=MP4))
    meeting = _meeting([_file("MP4", "active_speaker", *span, "https://zoom.us/v", len(MP4))])
    source, vtt, info = zoom.download_meeting("abc==", tmp_path, client=client, meeting=meeting)
    assert source == tmp_path / "source.mp4" and source.read_bytes() == MP4
    assert vtt is None and info["transcript"] is None


def test_download_refuses_a_recording_that_is_still_processing(zoom_env, tmp_path):
    span = ("2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z")
    meeting = _meeting([_file("MP4", "active_speaker", *span, "https://zoom.us/v", status="processing")])
    fake = FakeZoom()
    with pytest.raises(PipelineError) as err:
        zoom.download_meeting("abc==", tmp_path, client=_client(fake), meeting=meeting)
    assert err.value.code == "recording_not_ready" and err.value.retryable
    assert fake.api_requests() == []


def test_a_failed_download_leaves_no_parts_behind(zoom_env, tmp_path):
    span = ("2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z")
    client = _download_client(lambda r: httpx.Response(200, content=MP4))
    meeting = _meeting([_file("MP4", "active_speaker", *span, "https://zoom.us/v", len(MP4) + 1)])
    with pytest.raises(PipelineError):
        zoom.download_meeting("abc==", tmp_path, client=client, meeting=meeting)
    assert not (tmp_path / "tmp" / "zoom").exists() and not (tmp_path / "source.mp4").exists()


def _real_part(path, seconds, size="160x90", rate=25):
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def real_ffmpeg(monkeypatch):
    monkeypatch.setenv("FFMPEG_BIN", FFMPEG)
    monkeypatch.setenv("FFPROBE_BIN", FFPROBE)
    get_settings.cache_clear()


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="needs ffmpeg/ffprobe")
def test_concat_parts_joins_real_mp4s_losslessly(tmp_path, monkeypatch, real_ffmpeg):
    parts = [_real_part(tmp_path / "part_01.mp4", 2), _real_part(tmp_path / "part_02.mp4", 3)]
    monkeypatch.setattr(zoom, "_concat_reencoded", lambda *a: pytest.fail("same-encoding parts were re-encoded"))
    work = tmp_path / "work"
    work.mkdir()
    dest = tmp_path / "source.mp4"
    zoom.concat_parts(parts, dest, work)
    assert abs(zoom._probe_duration(parts[0]) - 2.0) < 0.1
    assert abs(zoom._probe_duration(dest) - 5.0) < 0.15


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="needs ffmpeg/ffprobe")
def test_concat_parts_reencodes_parts_that_differ_in_encoding(tmp_path, real_ffmpeg):
    # segments cut from different views can differ in size and rate; a stream
    # copy across them would not fail, it would write a corrupt file
    from slice.profile import probe_media

    parts = [_real_part(tmp_path / "part_01.mp4", 2), _real_part(tmp_path / "part_02.mp4", 3, "320x240", 30)]
    work = tmp_path / "work"
    work.mkdir()
    dest = tmp_path / "source.mp4"
    zoom.concat_parts(parts, dest, work)
    joined = probe_media(dest)
    assert (joined.width, joined.height, joined.fps) == (160, 90, 25)
    assert joined.has_audio and abs(joined.duration - 5.0) < 0.15
