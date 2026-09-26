"""Zoom cloud recordings as a job source (plan D13, §10).

A Server-to-Server OAuth app lists a host's cloud recordings, picks the one
MP4 view worth cutting plus Zoom's own transcript, and downloads both into
the job folder as `source.mp4` / `source.vtt`, so the rest of the pipeline
treats a Zoom meeting exactly like an upload with a transcript attached.

Rules that are easy to get wrong, and why they are here:
- S2S tokens last ~1 h and have no refresh token: cache the token with its
  expiry, re-mint a few minutes early, and re-mint once on a 401 (revoked or
  expired early). There is no user behind the token, so "me" never works;
  every call names the host email or the meeting instance UUID.
- Meeting UUIDs are base64 and may contain "/": Zoom wants those
  double-URL-encoded, everything else single-encoded. A numeric meeting id
  means "the latest instance" for recurring meetings, so it is never used.
- The Bearer token goes only to Zoom's own hosts over https. Download URLs
  redirect to storage/CDN hosts, and following them with the header attached
  would hand the account-wide token to a third party; redirect URLs (often
  signed) are never logged.
- A host who stops and restarts the recording gets several segments, each
  with its own views and transcript; the best view of every segment is joined
  in time order into one source, and each transcript is shifted by the
  measured length of the parts before it.
- The token endpoint's own outages and rate limits are Zoom's trouble, not a
  broken app: they are retried and then reported as retryable
  zoom_unavailable, never as zoom_auth.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx

from core.config import Settings, get_settings
from core.errors import InsufficientDisk, PipelineError, SourceUnsupported
from core.logging import get_logger
from core.proc import run_checked

logger = get_logger(__name__)
# httpx logs every request URL at INFO, which would copy signed CDN redirect
# URLs into job.log.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Best view first: the shared screen with the speaker inset shows both the
# content and who is talking; host_video is the last resort.
VIDEO_PRIORITY = (
    "shared_screen_with_speaker_view",
    "active_speaker",
    "shared_screen_with_gallery_view",
    "gallery_view",
    "shared_screen",
    "host_video",
)
REQUIRED_SCOPES = (
    "cloud_recording:read:list_user_recordings:admin",
    "cloud_recording:read:list_recording_files:admin",
    "cloud_recording:read:recording:admin",
)
NOT_CONFIGURED = "Zoom is not configured (fill ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET in .env)"

TOKEN_EARLY_REFRESH_S = 300
# The recordings list accepts at most a month per call.
MAX_WINDOW_DAYS = 30
# What POST /jobs/zoom tells callers to wait while Zoom is still processing.
NOT_READY_RETRY_AFTER_S = 300
# Zoom finishes a transcript within a few hours of the recording; a day later
# it is not coming (audio transcripts are off for that host or meeting).
TRANSCRIPT_OVERDUE_S = 24 * 3600
TRANSIENT_ATTEMPTS = 3
RATE_LIMIT_ATTEMPTS = 3
MAX_RETRY_AFTER_S = 60.0
# What callers are told to wait when Zoom is down (5xx) past our own retries.
UNAVAILABLE_RETRY_AFTER_S = 60
RESUME_ATTEMPTS = 3
MAX_REDIRECTS = 5
# How often a transcript wait runs the job checkpoint (heartbeat + cancel).
CHECKPOINT_EVERY_S = 5.0
# A stalled connection fails (and resumes) within a minute instead of wedging
# the single worker.
HTTP_TIMEOUT = httpx.Timeout(30.0, read=60.0)


class ZoomNotConfigured(PipelineError):
    code = "zoom_auth"


def is_configured(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool(s.zoom_account_id and s.zoom_client_id and s.zoom_client_secret)


def _auth_error(detail: str) -> PipelineError:
    return PipelineError(
        f"Zoom refused the request ({detail}). Check that the Server-to-Server OAuth app is ACTIVATED and has "
        f"the scopes {', '.join(REQUIRED_SCOPES)}.",
        code="zoom_auth",
    )


def _download_invalid(message: str) -> PipelineError:
    return PipelineError(message, code="zoom_download_invalid", retryable=True)


# ------------------------------------------------------------------ client


class ZoomClient:
    """Token handling, the two recordings endpoints and file downloads.

    `transport` lets tests plug in httpx.MockTransport; `sleep`/`clock` let
    them skip real waits."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or get_settings()
        if not is_configured(self.settings):
            raise ZoomNotConfigured(NOT_CONFIGURED)
        self._http = httpx.Client(transport=transport, timeout=HTTP_TIMEOUT, follow_redirects=False)
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self.scopes: list[str] = []

    # ------------------------------------------------------------- token
    def token(self, *, fresh: bool = False) -> str:
        with self._lock:
            if fresh or self._token is None or self._clock() >= self._expires_at:
                self._mint()
            return self._token

    def _mint(self) -> None:
        s = self.settings
        resp = self._send(
            lambda: self._http.build_request(
                "POST", s.zoom_oauth_url, data={"grant_type": "account_credentials", "account_id": s.zoom_account_id}
            ),
            auth=(s.zoom_client_id, s.zoom_client_secret),
        )
        # The token endpoint has outages and rate limits of its own. Past the
        # retries that is still Zoom's trouble, not a broken app: reporting it
        # as zoom_auth would stop n8n retrying and send the owner to the
        # Marketplace to "fix" an app that is fine.
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _unavailable(resp, "Zoom's token endpoint")
        body = _json(resp)
        if resp.status_code != 200 or not body.get("access_token"):
            reason = body.get("reason") or body.get("error") or f"HTTP {resp.status_code}"
            raise _auth_error(f"token request failed: {reason}")
        self._token = body["access_token"]
        expires_in = float(body.get("expires_in") or 3600)
        self._expires_at = self._clock() + max(0.0, expires_in - TOKEN_EARLY_REFRESH_S)
        self.scopes = str(body.get("scope") or "").split()

    # ------------------------------------------------------------- requests
    def _request(
        self, url: str, *, params: dict | None = None, headers: dict | None = None, with_token: bool = True,
        stream: bool = False,
    ) -> httpx.Response:
        """One GET, re-minting the token once on a 401 (revoked or expired
        early). The Bearer header is rebuilt per attempt so the retry after a
        re-mint carries the new token."""

        def build() -> httpx.Request:
            hdrs = dict(headers or {})
            if with_token:
                hdrs["Authorization"] = f"Bearer {self.token()}"
            return self._http.build_request("GET", url, params=params, headers=hdrs)

        return self._send(build, stream=stream, remint_on_401=with_token)

    def _send(
        self, build: Callable[[], httpx.Request], *, auth: tuple[str, str] | None = None, stream: bool = False,
        remint_on_401: bool = False,
    ) -> httpx.Response:
        """Send `build()` with Zoom's transient failures absorbed: a 429 waits
        Retry-After, 5xx and network errors back off, and with `remint_on_401`
        a 401 re-mints the token once. Shared by the token endpoint and the
        API. Returns the last response; the caller decides what a non-2xx
        means."""
        reminted = False
        transient = limited = 0
        while True:
            try:
                resp = self._http.send(build(), auth=auth, stream=stream)
            except httpx.TransportError as exc:
                transient += 1
                if transient > TRANSIENT_ATTEMPTS:
                    raise PipelineError(f"Zoom is unreachable: {exc}", code="zoom_unavailable", retryable=True,
                                        retry_after_s=UNAVAILABLE_RETRY_AFTER_S) from exc
                self._sleep(2.0**transient)
                continue
            if resp.status_code == 401 and remint_on_401 and not reminted:
                resp.close()
                reminted = True
                self.token(fresh=True)
                continue
            if resp.status_code == 429 and limited < RATE_LIMIT_ATTEMPTS:
                limited += 1
                wait = _retry_after(resp)
                resp.close()
                logger.info("Zoom rate limit hit; waiting %.0fs", wait)
                self._sleep(wait)
                continue
            if resp.status_code >= 500 and transient < TRANSIENT_ATTEMPTS:
                transient += 1
                resp.close()
                self._sleep(2.0**transient)
                continue
            return resp

    def _get_json(self, path: str, what: str, params: dict | None = None) -> dict:
        resp = self._request(self.settings.zoom_api_base.rstrip("/") + path, params=params)
        if resp.status_code == 200:
            return _json(resp)
        raise _api_error(resp, what)

    # ------------------------------------------------------------- endpoints
    def list_recordings(self, host_email: str, from_date: date, to_date: date) -> list[dict]:
        """Every cloud recording of `host_email` that started between the two
        dates (inclusive), newest first, as summaries (see `summarize`)."""
        meetings: dict[str, dict] = {}
        path = f"/users/{quote(host_email.strip(), safe='@')}/recordings"
        for start, end in date_windows(from_date, to_date):
            page_token = ""
            while True:
                params = {"from": start.isoformat(), "to": end.isoformat(), "page_size": 300}
                if page_token:
                    params["next_page_token"] = page_token
                data = self._get_json(path, f"recordings of {host_email}", params)
                for meeting in data.get("meetings") or []:
                    if meeting.get("uuid"):
                        meetings.setdefault(meeting["uuid"], meeting)
                page_token = data.get("next_page_token") or ""
                if not page_token:
                    break
        summaries = [summarize(m) for m in meetings.values()]
        summaries.sort(key=lambda s: s["start_time"] or "", reverse=True)
        return summaries

    def get_meeting(self, uuid: str) -> dict:
        """One meeting instance's recording files (always by instance UUID)."""
        return self._get_json(f"/meetings/{encode_uuid(uuid)}/recordings", f"recording {uuid}")

    # ------------------------------------------------------------- downloads
    def download_file(
        self, url: str, dest: Path, *, size: int | None = None, expect_mp4: bool = False,
        on_bytes: Callable[[int], None] | None = None,
    ) -> Path:
        """Stream one recording file to `dest` via `dest.part`, resuming with a
        Range request when the connection drops. `on_bytes(n)` gets the bytes
        of this file written so far (it may raise to cancel). Raises
        zoom_download_invalid when the result is not what Zoom described."""
        partial = dest.with_name(dest.name + ".part")
        partial.unlink(missing_ok=True)
        drops = 0
        while True:
            have = partial.stat().st_size if partial.exists() else 0
            if size and have >= size:
                break
            try:
                with self._open_download(url, {"Range": f"bytes={have}-"} if have else {}) as resp:
                    if have and resp.status_code != 206:
                        have = 0  # the server ignored the Range: start over
                    with partial.open("ab" if have else "wb") as out:
                        for chunk in resp.iter_bytes():
                            out.write(chunk)
                            have += len(chunk)
                            if on_bytes is not None:
                                on_bytes(have)
                break
            except httpx.TransportError as exc:
                drops += 1
                if drops > RESUME_ATTEMPTS:
                    raise _download_invalid(f"the Zoom download of {dest.name} kept dropping ({exc})") from exc
                logger.warning("Zoom download of %s dropped at %d bytes (%s); resuming (%d/%d)",
                               dest.name, have, exc, drops, RESUME_ATTEMPTS)
        _validate_download(partial, dest.name, size, expect_mp4)
        partial.replace(dest)
        return dest

    @contextmanager
    def _open_download(self, url: str, headers: dict):
        """A streaming 200/206 response for `url`, following redirects by hand
        so the token is attached only for Zoom's own https hosts."""
        for _ in range(MAX_REDIRECTS + 1):
            resp = self._request(url, headers=headers, with_token=self._may_send_token(url), stream=True)
            if not resp.is_redirect:
                break
            location = resp.headers.get("location", "")
            resp.close()
            url = urljoin(url, location)
        else:
            raise _download_invalid("the Zoom download redirected too many times")
        try:
            if resp.status_code not in (200, 206):
                raise _download_error(resp)
            yield resp
        finally:
            resp.close()

    def _may_send_token(self, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return parts.scheme == "https" and any(
            host == allowed or host.endswith("." + allowed) for allowed in self.settings.zoom_allowed_hosts
        )


_shared_lock = threading.Lock()
_shared: tuple[tuple, ZoomClient] | None = None


def get_client() -> ZoomClient:
    """One client per credential set for the whole process, so the ~1 h
    token is reused across API requests and jobs instead of minted per call."""
    global _shared
    s = get_settings()
    key = (s.zoom_account_id, s.zoom_client_id, s.zoom_client_secret, s.zoom_oauth_url, s.zoom_api_base)
    with _shared_lock:
        if _shared is None or _shared[0] != key:
            _shared = (key, ZoomClient(s))
        return _shared[1]


def encode_uuid(uuid: str) -> str:
    """Zoom's rule: a UUID that starts with "/" or contains "//" must be
    double-URL-encoded in the path; any other is encoded once."""
    once = quote(uuid, safe="")
    return quote(once, safe="") if uuid.startswith("/") or "//" in uuid else once


def date_windows(from_date: date, to_date: date) -> list[tuple[date, date]]:
    """Split [from_date, to_date] (inclusive) into windows of at most
    MAX_WINDOW_DAYS days, the most the recordings list accepts per call."""
    windows = []
    start = from_date
    while start <= to_date:
        end = min(to_date, start + timedelta(days=MAX_WINDOW_DAYS - 1))
        windows.append((start, end))
        start = end + timedelta(days=1)
    return windows


# ------------------------------------------------------------------ choosing files


@dataclass
class Part:
    """One recorded segment: its MP4 and the transcript covering it."""

    video: dict
    transcript: dict | None = None


@dataclass
class Choice:
    parts: list[Part] = field(default_factory=list)
    # "TRANSCRIPT" (Zoom's audio transcript), "CC" (closed captions, only when
    # there is no audio transcript) or None
    transcript_kind: str | None = None
    # segments that have audio or a transcript but no MP4 to cut (someone
    # deleted that file in Zoom, say): they can't be in the output, and the
    # job says so instead of quietly returning a shorter meeting
    segments_without_video: int = 0

    @property
    def video_type(self) -> str | None:
        """The view that is cut; several joined by "+" when the segments were
        recorded in different views."""
        return "+".join(dict.fromkeys(p.video["recording_type"] for p in self.parts)) or None

    @property
    def recording_ready(self) -> bool:
        return bool(self.parts) and all(_completed(p.video) for p in self.parts)

    @property
    def has_transcript(self) -> bool:
        return bool(self.parts) and all(p.transcript is not None and _completed(p.transcript) for p in self.parts)

    @property
    def verdict(self) -> str | None:
        """None when everything can be downloaded now, else the error code."""
        if not self.parts:
            return "source_unsupported"
        if not self.recording_ready:
            return "recording_not_ready"
        if not self.has_transcript:
            return "transcript_not_ready"
        return None


def choose_files(meeting: dict) -> Choice:
    """One part per recorded segment, in time order: the best view that
    segment has, and the transcript paired with it. Pure: decides from Zoom's
    file list only.

    The view is chosen per segment, not once per meeting, because Zoom names
    each segment's files by what happened in it: a host who stops and restarts
    the recording and only shares a screen in one segment gets
    shared_screen_with_speaker_view there and active_speaker elsewhere, and one
    type for the whole meeting would silently drop the other segments."""
    files = meeting.get("recording_files") or []
    videos = [f for f in files if _type(f) == "MP4"
              and f.get("recording_type") in VIDEO_PRIORITY]  # "(CC)" variants are not in the list
    if not videos:
        return Choice()
    chosen = [min(seg, key=lambda f: VIDEO_PRIORITY.index(f["recording_type"])) for seg in _segments(videos)]

    transcripts = [f for f in files if _type(f) == "TRANSCRIPT"]
    kind = "TRANSCRIPT" if transcripts else None
    if not transcripts:
        transcripts = [f for f in files if _type(f) == "CC"]
        kind = "CC" if transcripts else None
    paired = _pair(chosen, transcripts)

    # audio or a transcript that no MP4 covers is a segment missing its video
    # (a file without timestamps can't be placed, so it never counts)
    uncovered = [f for f in files if _type(f) in ("M4A", "TRANSCRIPT", "CC") and _span(f)
                 and not any(_same_segment(f, v) for v in videos)]
    return Choice([Part(v, t) for v, t in zip(chosen, paired, strict=True)], kind, len(_segments(uncovered)))


def _type(f: dict) -> str:
    return (f.get("file_type") or "").upper()


def _segments(files: list[dict]) -> list[list[dict]]:
    """Group recording files into recorded segments, in time order. Views of
    one segment cover the same span; separate segments barely touch (the
    recording was stopped in between). A file without usable timestamps can't
    be placed, so it joins the latest segment rather than becoming a part of
    its own (which would join the same footage twice)."""
    segments: list[list[dict]] = []
    for f in sorted(files, key=lambda f: (_span(f) is None, _start_key(f))):
        if segments and (_span(f) is None or _same_segment(f, segments[-1][0])):
            segments[-1].append(f)
        else:
            segments.append([f])
    return segments


def _same_segment(a: dict, b: dict) -> bool:
    """True when the files overlap by more than half the shorter one: views
    of one segment share their span, while a few seconds of clock jitter
    between two back-to-back segments never gets near half."""
    span_a, span_b = _span(a), _span(b)
    if span_a is None or span_b is None:
        return False
    shorter = min(span_a[1] - span_a[0], span_b[1] - span_b[0]).total_seconds()
    return _overlap_s(a, b) > shorter / 2


def _span(f: dict) -> tuple[datetime, datetime] | None:
    start, end = _parse_time(f.get("recording_start")), _parse_time(f.get("recording_end"))
    return (start, end) if start and end and end > start else None


def _pair(videos: list[dict], transcripts: list[dict]) -> list[dict | None]:
    """Give each MP4 the transcript whose recording span overlaps it most
    (each transcript used once). Only a lone pair where the timestamps can't
    tell is paired blind: with timestamps, a transcript that doesn't overlap
    the video belongs to another segment."""
    if len(videos) == 1 and len(transcripts) == 1 and None in (_span(videos[0]), _span(transcripts[0])):
        return [transcripts[0]]
    candidates = []
    for vi, video in enumerate(videos):
        for ti, transcript in enumerate(transcripts):
            overlap = _overlap_s(video, transcript)
            if overlap > 0:
                candidates.append((overlap, -vi, -ti))
    paired: list[dict | None] = [None] * len(videos)
    used: set[int] = set()
    for _, neg_vi, neg_ti in sorted(candidates, reverse=True):
        vi, ti = -neg_vi, -neg_ti
        if paired[vi] is None and ti not in used:
            paired[vi] = transcripts[ti]
            used.add(ti)
    return paired


def _overlap_s(a: dict, b: dict) -> float:
    a0, a1 = _parse_time(a.get("recording_start")), _parse_time(a.get("recording_end"))
    b0, b1 = _parse_time(b.get("recording_start")), _parse_time(b.get("recording_end"))
    if None in (a0, a1, b0, b1):
        return 0.0
    return (min(a1, b1) - max(a0, b0)).total_seconds()


def _completed(f: dict) -> bool:
    return (f.get("status") or "completed").lower() == "completed" and bool(f.get("download_url"))


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _start_key(f: dict) -> datetime:
    return _parse_time(f.get("recording_start")) or datetime.min.replace(tzinfo=UTC)


def transcript_overdue(meeting: dict, choice: Choice, now: datetime | None = None) -> bool:
    ends = [e for e in (_parse_time(p.video.get("recording_end")) for p in choice.parts) if e]
    if not ends:
        start = _parse_time(meeting.get("start_time"))
        if start is None:
            return False
        ends = [start + timedelta(minutes=float(meeting.get("duration") or 0))]
    return (now or datetime.now(UTC)) - max(ends) > timedelta(seconds=TRANSCRIPT_OVERDUE_S)


def readiness(meeting: dict, choice: Choice | None = None, now: datetime | None = None) -> str:
    """"ready", "recording_not_ready", "transcript_not_ready" (Zoom is still
    making it), "no_transcript" (it is not coming) or "source_unsupported"
    (no MP4 at all, e.g. an audio-only recording)."""
    choice = choice or choose_files(meeting)
    verdict = choice.verdict
    if verdict == "transcript_not_ready" and transcript_overdue(meeting, choice, now):
        return "no_transcript"
    return verdict or "ready"


def summarize(meeting: dict, now: datetime | None = None) -> dict:
    """One row of the recordings picker."""
    choice = choose_files(meeting)
    return {
        "uuid": meeting.get("uuid"),
        "meeting_id": meeting.get("id"),
        "topic": (meeting.get("topic") or "").strip(),
        "start_time": meeting.get("start_time"),
        "duration_min": meeting.get("duration"),
        "total_size": meeting.get("total_size"),
        "parts": len(choice.parts),
        "has_transcript": choice.has_transcript,
        "recording_ready": choice.recording_ready,
        "status": readiness(meeting, choice, now),
    }


def meeting_info(meeting: dict, choice: Choice | None = None) -> dict:
    """What the job keeps about the meeting (JobRecord.zoom_meeting)."""
    choice = choice or choose_files(meeting)
    return {
        "uuid": meeting.get("uuid"),
        "meeting_id": meeting.get("id"),
        "topic": (meeting.get("topic") or "").strip(),
        "start_time": meeting.get("start_time"),
        "host_email": meeting.get("host_email"),
        "duration_min": meeting.get("duration"),
        "auto_delete_date": meeting.get("auto_delete_date"),
        "recording_type": choice.video_type,
        "transcript": choice.transcript_kind if choice.has_transcript else None,
        "segments_without_video": choice.segments_without_video,
        "parts": [
            {
                "recording_type": p.video.get("recording_type"),
                "recording_start": p.video.get("recording_start"),
                "recording_end": p.video.get("recording_end"),
                "file_size": p.video.get("file_size"),
            }
            for p in choice.parts
        ],
    }


def not_ready_error(status: str) -> PipelineError:
    messages = {
        "recording_not_ready": "Zoom is still processing this recording",
        "transcript_not_ready": "Zoom has not finished this meeting's transcript yet",
        "no_transcript": "this recording has no Zoom transcript (turn on 'Create audio transcript' in Zoom's "
                         "cloud recording settings); this server does not transcribe recordings itself",
        "source_unsupported": "this recording has no MP4 video (audio-only?)",
    }
    if status == "source_unsupported":
        return SourceUnsupported(messages[status])
    code = "transcript_not_ready" if status == "no_transcript" else status
    retryable = status != "no_transcript"
    return PipelineError(messages.get(status, status), code=code, retryable=retryable,
                         retry_after_s=NOT_READY_RETRY_AFTER_S if retryable else None)


def wait_for_transcript(
    client: ZoomClient, uuid: str, meeting: dict, *, max_wait_s: float, poll_s: float,
    checkpoint: Callable[[], None] | None = None, sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Zoom makes the transcript minutes after the recording. Re-check every
    `poll_s` for up to `max_wait_s` rather than failing a job that was queued
    a little early; `checkpoint` runs every few seconds (heartbeat, cancel).
    Returns the latest meeting record either way."""
    deadline = clock() + max_wait_s
    while readiness(meeting) == "transcript_not_ready" and clock() < deadline:
        wake = min(deadline, clock() + max(poll_s, 1.0))
        while clock() < wake:
            if checkpoint is not None:
                checkpoint()
            sleep(min(CHECKPOINT_EVERY_S, max(0.0, wake - clock())))
        meeting = client.get_meeting(uuid)
    return meeting


# ------------------------------------------------------------------ downloading a meeting


def download_meeting(
    uuid: str,
    job_dir: Path,
    *,
    client: ZoomClient | None = None,
    meeting: dict | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    probe_duration: Callable[[Path], float] | None = None,
) -> tuple[Path, Path | None, dict]:
    """Download a meeting's chosen MP4 part(s) and transcript(s) into
    `job_dir` as source.mp4 and source.vtt (None when any part has no
    transcript yet). Returns (source.mp4, source.vtt or None, meeting info).

    `on_progress(done_bytes, total_bytes)` is called while downloading and
    may raise to cancel. Parts are staged under tmp/, which startup
    reconciliation clears if the service dies mid-download."""
    client = client or get_client()
    meeting = meeting if meeting is not None else client.get_meeting(uuid)
    choice = choose_files(meeting)
    if choice.verdict in ("source_unsupported", "recording_not_ready"):
        raise not_ready_error(choice.verdict)
    with_transcript = choice.has_transcript
    files = [(p.video, True) for p in choice.parts]
    if with_transcript:
        files += [(p.transcript, False) for p in choice.parts]
    total = sum(int(f.get("file_size") or 0) for f, _ in files)
    multi = len(choice.parts) > 1
    _ensure_disk(job_dir, total * (2 if multi else 1))

    work = job_dir / "tmp" / "zoom"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    done = 0

    def fetch(f: dict, dest: Path, is_video: bool) -> Path:
        nonlocal done
        base = done

        def on_bytes(n: int) -> None:
            if on_progress is not None:
                on_progress(base + n, total)

        path = client.download_file(f["download_url"], dest, size=int(f.get("file_size") or 0) or None,
                                    expect_mp4=is_video, on_bytes=on_bytes)
        done = base + path.stat().st_size
        return path

    # a failed or cancelled download must not leave gigabytes of parts behind
    try:
        videos = [fetch(p.video, work / f"part_{i:02d}.mp4", True) for i, p in enumerate(choice.parts, 1)]
        vtts = [fetch(p.transcript, work / f"part_{i:02d}.vtt", False) for i, p in enumerate(choice.parts, 1)
                ] if with_transcript else []

        info = meeting_info(meeting, choice)
        source = job_dir / "source.mp4"
        offsets = [0.0]
        if multi:
            probe = probe_duration or _probe_duration
            durations = [probe(v) for v in videos]
            offsets = [sum(durations[:i]) for i in range(len(videos))]
            for part, offset, duration in zip(info["parts"], offsets, durations, strict=True):
                part.update(offset_s=round(offset, 3), duration_s=round(duration, 3))
            concat_parts(videos, source, work)
            logger.info("joined %d Zoom recording parts (%s) into %s", len(videos),
                        ", ".join(f"{d:.1f}s" for d in durations), source.name)
        else:
            videos[0].replace(source)

        vtt_path = None
        if vtts:
            vtt_path = job_dir / "source.vtt"
            texts = [v.read_text(encoding="utf-8-sig", errors="replace") for v in vtts]
            vtt_path.write_text(combine_vtts(texts, offsets), encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return source, vtt_path, info


def _ensure_disk(folder: Path, needed: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    reserve = max(0, get_settings().min_free_disk_bytes)
    free = shutil.disk_usage(folder).free
    if free < needed + reserve:
        raise InsufficientDisk(
            f"the Zoom recording needs {needed / 1024**3:.1f} GiB but only {free / 1024**3:.1f} GiB is free "
            f"(keeping {reserve / 1024**3:.1f} GiB spare)"
        )


def _probe_duration(path: Path) -> float:
    from slice.profile import ffprobe_json

    return float(ffprobe_json(path).get("format", {}).get("duration") or 0.0)


def concat_parts(parts: list[Path], dest: Path, work: Path) -> None:
    """Join Zoom parts in order: losslessly (concat demuxer, stream copy) when
    they share their encoding, which parts of one recording setup normally
    do; otherwise (segments cut from different views, say) re-encoded with the
    pinned profile, because a stream copy across differing SPS/PPS or sizes
    does not fail, it writes a silently corrupt file (see slice/profile.py)."""
    settings = get_settings()
    joined = work / "joined.mp4"
    if _same_encoding(parts):
        listing = work / "concat.txt"
        listing.write_text(
            "".join("file '" + p.resolve().as_posix().replace("'", "'\\''") + "'\n" for p in parts), encoding="utf-8"
        )
        run_checked(
            [settings.ffmpeg_bin, "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", str(joined)],
            timeout=settings.ffmpeg_timeout_seconds,
        )
    else:
        _concat_reencoded(parts, joined)
    joined.replace(dest)


def _same_encoding(parts: list[Path]) -> bool:
    from slice.profile import probe_header

    def key(path: Path) -> tuple:
        h = probe_header(path)
        return (h.extradata_hash, h.fps, h.width, h.height, h.pix_fmt, h.audio_codec, h.audio_sample_rate,
                h.audio_channels)

    return len({key(p) for p in parts}) == 1


def _concat_reencoded(parts: list[Path], dest: Path) -> None:
    """Every part scaled (letterboxed) to the first part's size and joined by
    the concat filter, which settles the audio format itself; the output has
    the first part's frame rate, constant."""
    from slice.profile import AUDIO_ARGS, BASE_ARGS, probe_media, video_args

    settings = get_settings()
    first = probe_media(parts[0])
    w, h = first.even_width, first.even_height
    inputs, chains, pads = [], [], ""
    for i, part in enumerate(parts):
        inputs += ["-i", str(part)]
        chains.append(f"[{i}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                      f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]")
        pads += f"[v{i}][{i}:a:0]"
    graph = ";".join([*chains, f"{pads}concat=n={len(parts)}:v=1:a=1[v][a]"])
    content_s = sum(_probe_duration(p) for p in parts)
    logger.info("the %d Zoom parts differ in encoding; re-encoding them to join (%.0fs of video)",
                len(parts), content_s)
    run_checked(
        [settings.ffmpeg_bin, *BASE_ARGS, *inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
         *video_args(first.fps), *AUDIO_ARGS, str(dest)],
        # a whole meeting, not one piece: the usual per-piece cap is too short
        timeout=max(settings.ffmpeg_timeout_seconds, settings.ffmpeg_timeout_per_content_second * content_s),
    )


_VTT_TIME = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{1,3})")


def _shift_stamp(match: re.Match, offset_s: float) -> str:
    h, m, s, frac = match.groups()
    seconds = int(h or 0) * 3600 + int(m) * 60 + int(s) + int(frac.ljust(3, "0")) / 1000 + offset_s
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def shift_vtt(text: str, offset_s: float) -> str:
    """The cues of a WebVTT file (header dropped) with every timing line
    moved by `offset_s`."""
    lines = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[0].startswith("WEBVTT"):
        lines = lines[1:]
    out = [_VTT_TIME.sub(lambda m: _shift_stamp(m, offset_s), ln) if "-->" in ln else ln for ln in lines]
    return "\n".join(out).strip("\n")


def combine_vtts(texts: list[str], offsets: list[float]) -> str:
    """One WebVTT for the joined recording: part i's cues shifted by the
    measured length of the parts before it."""
    bodies = [shift_vtt(text, offset) for text, offset in zip(texts, offsets, strict=True)]
    return "WEBVTT\n\n" + "\n\n".join(b for b in bodies if b) + "\n"


# ------------------------------------------------------------------ errors / helpers


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _zoom_detail(resp: httpx.Response) -> str:
    body = _json(resp)
    message = body.get("message") or body.get("reason") or body.get("error") or ""
    code = body.get("code")
    return f"HTTP {resp.status_code}" + (f", code {code}" if code else "") + (f": {message}" if message else "")


def _api_error(resp: httpx.Response, what: str) -> PipelineError:
    detail = _zoom_detail(resp)
    body = _json(resp)
    # a token without the needed scopes is answered 400 (code 4711), not 403
    if resp.status_code in (401, 403) or body.get("code") in (4700, 4711) or "scope" in detail.lower():
        return _auth_error(detail)
    if resp.status_code == 404:
        return PipelineError(f"Zoom: {what} not found ({detail})", code="zoom_not_found")
    if resp.status_code == 429 or resp.status_code >= 500:
        return _unavailable(resp, f"Zoom's API for {what}")
    return PipelineError(f"Zoom API error for {what} ({detail})")


def _unavailable(resp: httpx.Response, what: str) -> PipelineError:
    """Zoom still rate-limiting or failing after our own retries: retryable,
    with the wait Zoom asked for (429) or a short default (5xx)."""
    if resp.status_code == 429:
        return PipelineError(f"{what} is rate-limiting us ({_zoom_detail(resp)})", code="zoom_unavailable",
                             retryable=True, retry_after_s=int(_retry_after(resp)))
    return PipelineError(f"{what} is failing ({_zoom_detail(resp)}); try again shortly", code="zoom_unavailable",
                         retryable=True, retry_after_s=UNAVAILABLE_RETRY_AFTER_S)


def _download_error(resp: httpx.Response) -> PipelineError:
    detail = f"HTTP {resp.status_code}"
    if resp.status_code in (401, 403):
        return _auth_error(f"download refused, {detail}")
    if resp.status_code == 404:
        return PipelineError(f"the Zoom recording file is gone ({detail}; deleted or in the trash?)",
                             code="zoom_not_found")
    return _download_invalid(f"the Zoom download failed ({detail})")


def _retry_after(resp: httpx.Response) -> float:
    try:
        wait = float(resp.headers.get("retry-after", ""))
    except ValueError:
        wait = 10.0
    return min(max(wait, 1.0), MAX_RETRY_AFTER_S)


def _validate_download(path: Path, name: str, size: int | None, expect_mp4: bool) -> None:
    got = path.stat().st_size
    if size and got != size:
        raise _download_invalid(f"the Zoom download of {name} is {got} bytes, Zoom said {size}")
    if got == 0:
        raise _download_invalid(f"the Zoom download of {name} is empty")
    if expect_mp4:
        with path.open("rb") as f:
            head = f.read(12)
        if head[4:8] != b"ftyp":
            raise _download_invalid(f"the Zoom download of {name} is not an MP4 (no ftyp box)")
