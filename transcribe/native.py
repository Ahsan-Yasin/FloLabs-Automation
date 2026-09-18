import json
import re
import time
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.models import Segment, Word

logger = get_logger(__name__)

# yt-dlp's default socket timeout (20s) isn't always enough for its metadata
# request — seen failing in production with a plain read timeout, most likely
# because run_youtube_pipeline already made a full video-download request to
# YouTube moments earlier and newer yt-dlp releases make more requests per
# extract_info call than older ones did. Retry once before giving up, since a
# transient timeout is far more likely than the caption track genuinely
# vanishing between the download and this call.
_YOUTUBE_FETCH_ATTEMPTS = 2
_YOUTUBE_FETCH_RETRY_DELAY_SECONDS = 3.0
_YOUTUBE_FETCH_SOCKET_TIMEOUT_SECONDS = 30

_TIMESTAMP = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})")
_TAG = re.compile(r"<[^>]+>")
_VOICE_TAG = re.compile(r"<v(?:\.[\w-]+)*\s+([^>]+)>")
_SPEAKER_PREFIX = re.compile(r"^([A-Za-z][\w .'\-]{0,40}):\s+(.*)$")

DEFAULT_SPEAKER = "SPEAKER"

_SENTENCE_END = re.compile(r'[.!?]+["\')\]]*$')
_MAX_SEGMENT_WORDS = 40
_MAX_SEGMENT_SECONDS = 20.0


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _interpolate(cue: Word, token_index: int, tokens_in_cue: int, edge: str) -> float:
    """A proportional timestamp for one token's position within its cue's span.

    There's no real sub-cue timing to draw on, so this is an estimate — but a
    monotonically increasing one, which is what actually matters: it's the
    only thing standing between two sentences carved out of the same cue and
    them ending up with IDENTICAL start/end. Degenerate identical timestamps
    are what actually breaks the EDL (see _group_into_sentences docstring),
    not the estimate being imprecise.
    """
    if tokens_in_cue <= 1:
        return cue.start if edge == "start" else cue.end
    frac = token_index / tokens_in_cue if edge == "start" else (token_index + 1) / tokens_in_cue
    return cue.start + frac * (cue.end - cue.start)


def _group_into_sentences(cues: list[Word]) -> list[Segment]:
    """Recombine cue-level pseudo-words into sentence-level segments.

    Caption cues are timed for on-screen readability, not for being
    linguistic units — a single sentence routinely gets split across two or
    three consecutive cues (e.g. "I proceed down, let's check if we have" /
    "our new members here haven't introduced" / "themselves to the team."),
    and a single cue can just as easily contain several short, complete
    sentences. Handing the LLM those arbitrary display-driven fragments
    instead of whole sentences makes it harder to judge keep/remove
    correctly. This regroups the same cues at sentence boundaries — and
    always at a speaker change, so dialogue never gets misattributed — using
    the fine cue-level Word list (kept separate, unchanged) for precise EDL
    snapping. The word/duration cap guards against tracks with sparse or
    missing punctuation producing one runaway segment.

    Sentences carved out of the SAME cue get proportionally interpolated
    sub-timestamps (see _interpolate) rather than all inheriting that cue's
    whole span. Without this, e.g. "Yes." / "Yes, I did." / "I introduced
    myself." from one cue would all get identical start/end — and if the LLM
    keeps one of those three and removes another, the kept one's range
    silently overrides the removed one's for that exact span, and can chain
    through overlap-coalescing into merging huge, unrelated stretches of the
    video back together. This was found and fixed by tracing a real EDL
    build where a single 35-minute "keep" block appeared before any
    threshold-based merging had even run — only possible from exactly this
    kind of overlapping-timestamp collision.
    """
    tokens: list[tuple[str, Word, int, int]] = []
    for cue in cues:
        cue_tokens = cue.word.split()
        n = len(cue_tokens)
        for i, tok in enumerate(cue_tokens):
            tokens.append((tok, cue, i, n))

    segments: list[Segment] = []
    current: list[tuple[str, Word, int, int]] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join(tok for tok, _, _, _ in current)
        _, first_cue, first_idx, first_total = current[0]
        _, last_cue, last_idx, last_total = current[-1]
        start = _interpolate(first_cue, first_idx, first_total, "start")
        end = max(_interpolate(last_cue, last_idx, last_total, "end"), start)
        segments.append(Segment(speaker=first_cue.speaker, start=start, end=end, text=text))

    for tok, cue, idx, total in tokens:
        if current and cue.speaker != current[-1][1].speaker:
            flush()
            current = []

        current.append((tok, cue, idx, total))

        duration = current[-1][1].end - current[0][1].start
        if _SENTENCE_END.search(tok) or len(current) >= _MAX_SEGMENT_WORDS or duration >= _MAX_SEGMENT_SECONDS:
            flush()
            current = []

    flush()
    return segments


def parse_subtitle_text(text: str, default_speaker: str = DEFAULT_SPEAKER) -> tuple[list[Word], list[Segment]]:
    """Parse a WebVTT or SRT file into cue-level Words and sentence-level Segments.

    Used for user-supplied transcripts (e.g. exported from Zoom), which are
    clean single-pass cues — unlike YouTube's rolling auto-caption VTT, so no
    dedup pass is needed here. Each cue becomes one pseudo-"word" (its full
    text) — there's no finer timing than the subtitle track provides, so
    downstream cuts snap to cue boundaries, not individual words — while
    Segments are cues regrouped at sentence boundaries (see
    `_group_into_sentences`) so the LLM judges whole sentences.
    """
    blocks = re.split(r"\r?\n\r?\n+", text.strip())
    words: list[Word] = []

    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        ts_index = None
        match = None
        for i, line in enumerate(lines):
            m = _TIMESTAMP.search(line)
            if m:
                ts_index = i
                match = m
                break
        if match is None:
            continue

        start = _to_seconds(*match.group(1, 2, 3, 4))
        end = _to_seconds(*match.group(5, 6, 7, 8))
        if end <= start:
            continue

        content_lines = lines[ts_index + 1 :]
        if not content_lines:
            continue

        speaker = default_speaker
        voice_match = _VOICE_TAG.search(content_lines[0])
        if voice_match:
            speaker = voice_match.group(1).strip()

        cleaned = [_TAG.sub("", ln).strip() for ln in content_lines]
        cleaned = [ln for ln in cleaned if ln]
        if not cleaned:
            continue

        if not voice_match:
            prefix_match = _SPEAKER_PREFIX.match(cleaned[0])
            if prefix_match:
                speaker = prefix_match.group(1).strip()
                cleaned[0] = prefix_match.group(2).strip()
                cleaned = [ln for ln in cleaned if ln]
                if not cleaned:
                    continue

        full_text = " ".join(cleaned).strip()
        if not full_text:
            continue

        words.append(Word(word=full_text, start=start, end=end, speaker=speaker))

    words.sort(key=lambda w: w.start)
    segments = _group_into_sentences(words)
    return words, segments


def _parse_json3(data: dict, default_speaker: str = DEFAULT_SPEAKER) -> tuple[list[Word], list[Segment]]:
    """Parse YouTube's json3 caption format — sequential, non-overlapping events,
    unlike the rolling/karaoke-style VTT export of the same track."""
    words: list[Word] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        start_ms = event.get("tStartMs")
        dur_ms = event.get("dDurationMs")
        if not segs or start_ms is None or dur_ms is None:
            continue

        text = "".join(seg.get("utf8", "") for seg in segs).replace("\n", " ").strip()
        if not text:
            continue

        start = start_ms / 1000.0
        end = (start_ms + dur_ms) / 1000.0
        if end <= start:
            continue

        words.append(Word(word=text, start=start, end=end, speaker=default_speaker))

    words.sort(key=lambda w: w.start)
    segments = _group_into_sentences(words)
    return words, segments


def segments_to_words(segments: list[Segment]) -> list[Word]:
    """Turn sentence-level segments into the "word" granularity the rest of
    the pipeline (EDL snap-to-boundary, transcript output, overlap
    detection) works with — for a native-transcript source, a sentence is
    the finest meaningful unit we actually have, now that multi-sentence
    cues are split with real distinct timestamps (see
    _group_into_sentences). Snapping EDL cuts against the original, coarser
    per-cue Word list instead would expand an interpolated mid-cue cut back
    out to that cue's full span, silently undoing the split.
    """
    return [
        Word(word=s.text, start=s.start, end=s.end, speaker=s.speaker, overlap_candidate=s.overlap_candidate)
        for s in segments
    ]


def _pick_track(manual: dict, automatic: dict) -> dict | None:
    """Prefer creator-uploaded captions over auto-generated ones, and prefer the
    json3 format (clean timing) over vtt (rolling/karaoke for auto-captions)."""
    for source in (manual, automatic):
        for lang, entries in source.items():
            if lang == "en" or lang.startswith(("en-", "en_")):
                for ext in ("json3", "vtt"):
                    for entry in entries:
                        if entry.get("ext") == ext:
                            return entry
    return None


def fetch_youtube_transcript(url: str) -> tuple[list[Word], list[Segment]] | None:
    """Try to reuse YouTube's own captions instead of running WhisperX.

    Never raises — returns None on any failure or missing caption track so
    callers can fall back to self-hosted transcription. Retries once on a
    transient network error (see _YOUTUBE_FETCH_ATTEMPTS) before giving up.
    """
    import yt_dlp

    settings = get_settings()
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": _YOUTUBE_FETCH_SOCKET_TIMEOUT_SECONDS,
    }
    ffmpeg_path = Path(settings.ffmpeg_bin)
    if ffmpeg_path.is_file():
        ydl_opts["ffmpeg_location"] = str(ffmpeg_path.parent)

    result = None
    last_exc = None
    for attempt in range(1, _YOUTUBE_FETCH_ATTEMPTS + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                track = _pick_track(info.get("subtitles") or {}, info.get("automatic_captions") or {})
                if track is None:
                    logger.info("no usable English caption track for %s — will transcribe ourselves", url)
                    return None

                with ydl.urlopen(track["url"]) as resp:
                    raw = resp.read()

                if track.get("ext") == "json3":
                    result = _parse_json3(json.loads(raw))
                else:
                    result = parse_subtitle_text(raw.decode("utf-8", errors="replace"))
            break
        except Exception as exc:  # noqa: BLE001 — yt-dlp/network/parse errors all fall back, never fail the job
            last_exc = exc
            if attempt < _YOUTUBE_FETCH_ATTEMPTS:
                logger.warning(
                    "YouTube caption fetch attempt %d/%d failed for %s: %s — retrying",
                    attempt,
                    _YOUTUBE_FETCH_ATTEMPTS,
                    url,
                    exc,
                )
                time.sleep(_YOUTUBE_FETCH_RETRY_DELAY_SECONDS)

    if result is None:
        logger.warning("YouTube caption fetch failed for %s: %s — will transcribe ourselves", url, last_exc)
        return None

    words, segments = result
    if not words:
        return None

    logger.info("using YouTube's own captions for %s (%d cues) — skipping WhisperX", url, len(words))
    return words, segments


def load_uploaded_transcript(path: Path) -> tuple[list[Word], list[Segment]] | None:
    """Parse a user-supplied .vtt/.srt (e.g. exported from Zoom). Returns None
    (never raises) on any parse failure so callers fall back to WhisperX."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        words, segments = parse_subtitle_text(text)
    except Exception as exc:  # noqa: BLE001 — malformed file falls back, never fails the job
        logger.warning("failed to parse uploaded transcript %s: %s — will transcribe ourselves", path, exc)
        return None

    if not words:
        return None

    logger.info("using uploaded transcript %s (%d cues) — skipping WhisperX", path, len(words))
    return words, segments
