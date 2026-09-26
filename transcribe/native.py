"""Reuse a transcript the platform already made instead of running WhisperX.

WhisperX is by far the most expensive stage, and two sources usually ship
one already:

- Zoom writes a timed, speaker-attributed ``audio_transcript.vtt`` for most
  cloud recordings (and an upload can bring its own .vtt/.srt). The parser has
  to be forgiving about what real files look like — CRLF endings, a BOM,
  optional cue identifiers, cue settings on the timing line, multi-line cue
  text, ``<v Name>`` voice tags from other tools — and careful about Zoom's
  speaker convention: the speaker is a plain ``Display Name: text`` prefix, an
  unattributed cue has no prefix at all, and nothing distinguishes that prefix
  from a colon the speaker actually said. See ``_resolve_speaker`` for how the
  two are told apart.
- YouTube videos usually have English captions, creator-uploaded or
  auto-generated, fetched through yt-dlp. They carry no speakers. The json3
  export is preferred because the VTT export of auto-captions is "rolling"
  (every cue repeats the line before it); the VTT fallback undoes that.

Both paths end the same way — cue-level Words plus sentence-level Segments
(see ``_group_into_sentences``, which is load-bearing for the EDL) — and both
return None rather than raise, so the pipeline falls back to WhisperX.
"""

import html
import json
import re
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.logging import get_logger
from core.models import Segment, Word

logger = get_logger(__name__)

# Hours are optional in WebVTT (MM:SS.mmm) and some exporters write fewer than
# three fraction digits, so both are tolerated. Anything after the end
# timestamp (cue settings such as "align:start position:0%") is ignored.
_TIME = r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{1,3})"
_TIMESTAMP = re.compile(_TIME + r"\s*-->\s*" + _TIME)
_TAG = re.compile(r"<[^>]+>")
_VOICE_TAG = re.compile(r"<v(?:\.[\w-]+)*\s+([^>]+)>")
# A candidate "Name: text" prefix. The name can hold anything a Zoom display
# name can ("Smith, John", "Ahsan (FloLabs)", "jdoe@example.com", accents) but
# never a colon or sentence-ending punctuation. The 64-char cap is looser than
# _MAX_NAME_CHARS on purpose: a long name ("Graham Robinson | Head of
# Engineering, FloLabs") fails the one-off display-name shape test but must
# still be counted, so that it's accepted once it recurs.
_SPEAKER_PREFIX = re.compile(r"^([^:?!\s][^:?!]{0,63}?)\s*:\s+(\S.*)$")
_HANDLE = re.compile(
    r"^@?[\w.+-]+@[\w-]+(?:\.[\w-]+)+$"  # email address
    r"|^@\w[\w.-]*$"  # @handle
    r"|^[a-z][a-z0-9]+(?:[._-][a-z0-9]+)+$"  # jdoe_42, ahsan.ali
    r"|^[a-z]+\d+$",  # user123
    re.IGNORECASE,
)
_NON_WORD = re.compile(r"[^\w\s]")

# Unknown speaker: Zoom's unattributed cues, prefix-less cues from other
# tools, any "X: text" line whose X was judged not to be a speaker, and every
# YouTube caption.
DEFAULT_SPEAKER = "SPEAKER"

# Things people say or write before a colon that are never display names.
# Compared after casefolding and stripping punctuation, so "P.S." == "ps" and
# "TL;DR" == "tldr". For a prefix seen only once the first word is checked
# too, so "Thanks Everyone: ..." is rejected; a recurring prefix only has to
# clear the whole-label check, because a real first name can be a stop word
# ("So Yeon Park").
_PREFIX_STOP_LIST = frozenset(
    {
        "note", "notes", "question", "answer", "step", "update", "updates", "example", "warning", "important",
        "okay", "ok", "so", "yes", "no", "well", "also", "but", "and", "hi", "hello", "thanks", "thank you",
        "ps", "fyi", "tldr", "agenda", "action item", "action items", "next steps", "reminder", "todo", "summary",
        # generic labels that read as capitalised one-word "names" otherwise
        "problem", "solution", "context", "background", "goal", "result", "status", "decision", "recap",
        "side note", "quick question", "bottom line", "key takeaways", "takeaway", "tip", "correction",
    }
)  # fmt: skip
# Lowercase particles that legitimately sit inside a capitalised name
# ("Zoë van der Berg", "María de la Cruz").
_NAME_PARTICLES = frozenset(
    {"de", "da", "del", "della", "der", "den", "di", "du", "la", "le", "van", "von", "bin", "ibn", "al", "el",
     "y", "e", "dos", "das", "do", "st", "ter", "ten", "zu"}
)  # fmt: skip
_MAX_NAME_TOKENS = 4
_MAX_NAME_CHARS = 40

_SENTENCE_END = re.compile(r'[.!?]+["\')\]]*$')
_MAX_SEGMENT_WORDS = 40
_MAX_SEGMENT_SECONDS = 20.0

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

# YouTube's rolling VTT re-shows each finished line in a 10ms cue between two
# real ones. Real speech never fits in a cue this short, so a repeat in one is
# always that transition and never a line someone said twice.
_ROLLING_TRANSITION_MAX_SECONDS = 0.05


# ------------------------------------------------------------------ sentences


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


# ------------------------------------------------------------------ subtitle files


@dataclass
class _RawCue:
    """One timed cue before speaker resolution: its text split into runs,
    where a run is a stretch of text under one ``<v Name>`` voice (None when
    the text carries no voice tag and may still hold a "Name:" prefix)."""

    start: float
    end: float
    lines: list[str]
    runs: list[tuple[str | None, str]] | None = None


def _to_seconds(h: str | None, m: str, s: str, frac: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(frac) / 10 ** len(frac)


def _clean_text(raw: str) -> str:
    """Cue text as plain words: markup (<i>, <c>, </v>, karaoke timestamp
    tags) stripped, whitespace collapsed, and VTT's escapes (&amp;, &gt;)
    decoded — after stripping, so an escaped "&lt;" isn't taken for a tag."""
    return html.unescape(" ".join(_TAG.sub("", raw).split()))


def _read_cues(text: str) -> list[_RawCue]:
    """Split subtitle text into timed cues, line by line.

    A timing line opens a cue and a blank line closes it; everything else
    outside a cue (the WEBVTT header, NOTE/STYLE blocks, SRT/VTT cue
    identifiers) is skipped, which is what makes identifiers optional. A
    missing blank line between cues is tolerated: the next timing line still
    opens a new cue, and a bare number just before it is treated as that
    cue's identifier rather than as speech. A whitespace-only line straight
    after the timing line doesn't close the cue: YouTube's rolling VTT opens
    cues that way, and closing there would lose the cue's actual text.
    """
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[_RawCue] = []
    current: _RawCue | None = None
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        match = _TIMESTAMP.search(line) if "-->" in line else None
        if match:
            if current is not None and current.lines and current.lines[-1].isdigit():
                current.lines.pop()
            current = _RawCue(
                start=_to_seconds(*match.group(1, 2, 3, 4)),
                end=_to_seconds(*match.group(5, 6, 7, 8)),
                lines=[],
            )
            cues.append(current)
        elif not line:
            if raw_line and current is not None and not current.lines:
                continue
            current = None
        elif current is not None:
            current.lines.append(line)
    return [c for c in cues if c.end > c.start and c.lines]


def _voice_runs(lines: list[str]) -> list[tuple[str | None, str]]:
    """Split a cue's text at ``<v Name>`` voice tags into (voice, text) runs.

    Text before the first voice tag gets voice None. Other markup is stripped
    (see _clean_text), so a multi-line cue reads as one line. Adjacent runs
    with the same voice merge.
    """
    joined = "\n".join(lines)
    pieces: list[tuple[str | None, str]] = []
    voice: str | None = None
    pos = 0
    for match in _VOICE_TAG.finditer(joined):
        pieces.append((voice, joined[pos : match.start()]))
        voice = " ".join(match.group(1).split()) or None
        pos = match.end()
    pieces.append((voice, joined[pos:]))

    runs: list[tuple[str | None, str]] = []
    for run_voice, raw in pieces:
        clean = _clean_text(raw)
        if not clean:
            continue
        if runs and runs[-1][0] == run_voice:
            runs[-1] = (run_voice, f"{runs[-1][1]} {clean}")
        else:
            runs.append((run_voice, clean))
    return runs


# ------------------------------------------------------------------ Zoom speakers


def _normalise_label(label: str) -> str:
    return " ".join(_NON_WORD.sub("", label.casefold()).split())


def _is_name_token(token: str) -> bool:
    """A non-first token of a display name: capitalised ("Smith", "O'Neil",
    "iPhone"), a number ("Room 3"), a bracketed tag ("(FloLabs)",
    "(they/them)"), a separator, or a name particle ("van", "de")."""
    if token.startswith(("(", "[")):
        return True
    core = token.strip("\"'.,-|")
    if not core or core.casefold() in _NAME_PARTICLES:
        return True
    return not (core[0].isalpha() and core.islower())


def _looks_like_display_name(name: str) -> bool:
    """Heuristic shape of a Zoom display name: 1-4 capitalised tokens
    ("Graham Robinson", "Dr. Smith", "Smith, John", "María José García",
    "Hareem's iPhone") or a single email/handle. Deliberately stricter than
    "first word is capitalised" so a sentence that happens to open with a
    colon ("Here's the thing: ...") keeps its words instead of losing them to
    a made-up speaker. A real name that fails this shape is still accepted
    once it recurs as a prefix (see _resolve_speaker)."""
    tokens = name.split()
    if not 1 <= len(tokens) <= _MAX_NAME_TOKENS or len(name) > _MAX_NAME_CHARS:
        return False
    if not any(ch.isalpha() for ch in name):
        return False
    if len(tokens) == 1 and _HANDLE.match(tokens[0]):
        return True
    first = tokens[0].lstrip("(['\"")
    if not first[:1].isupper():
        return False
    return all(_is_name_token(t) for t in tokens[1:])


def _prefix_candidate(text: str) -> tuple[str, str] | None:
    match = _SPEAKER_PREFIX.match(text)
    if not match:
        return None
    return " ".join(match.group(1).split()), match.group(2).strip()


def _resolve_speaker(text: str, prefix_counts: Counter[str], default_speaker: str) -> tuple[str, str]:
    """Split a Zoom-style "Name: text" line into (speaker, text).

    Zoom writes the speaker as a plain prefix, which is indistinguishable
    from a colon in speech. A label ("Note:", "Action items:") is never a
    speaker. Otherwise "X: text" is a speaker when X recurs as a prefix at
    least twice in the file (real participants speak more than once), or
    when X looks like a display name on its own (someone who spoke once) and
    doesn't open with a common opener ("Thanks Everyone:"). Recurrence wins
    over the shape and opener checks, which only exist to judge a one-off
    prefix. Anything else keeps the whole line as text under the
    unknown-speaker label.
    """
    candidate = _prefix_candidate(text)
    if candidate is None:
        return default_speaker, text
    name, rest = candidate
    label = _normalise_label(name)
    if not label or label in _PREFIX_STOP_LIST:
        return default_speaker, text
    if prefix_counts[name] >= 2:
        return name, rest
    if label.split()[0] not in _PREFIX_STOP_LIST and _looks_like_display_name(name):
        return name, rest
    return default_speaker, text


def _split_span(start: float, end: float, texts: list[str]) -> list[tuple[float, float]]:
    """Share one cue's span between several runs in proportion to word count,
    so two voices in one cue get distinct, contiguous timings instead of the
    identical overlapping span that breaks the EDL (see _interpolate)."""
    counts = [max(len(t.split()), 1) for t in texts]
    total = sum(counts)
    spans: list[tuple[float, float]] = []
    elapsed = 0
    for count in counts:
        span_start = start + (end - start) * elapsed / total
        elapsed += count
        spans.append((span_start, start + (end - start) * elapsed / total))
    spans[-1] = (spans[-1][0], end)
    return spans


# ------------------------------------------------------------------ Zoom / uploaded transcripts


def parse_subtitle_text(
    text: str,
    default_speaker: str = DEFAULT_SPEAKER,
    max_duration_s: float | None = None,
) -> tuple[list[Word], list[Segment]]:
    """Parse a WebVTT or SRT file into cue-level Words and sentence-level Segments.

    Used for Zoom's audio transcript and user-supplied transcripts, which are
    clean single-pass cues, so no dedup pass is needed here (YouTube's rolling
    VTT goes through _parse_youtube_vtt instead). Each cue becomes one
    pseudo-"word" (its full text) — there's no finer timing than the subtitle
    track provides, so downstream cuts snap to cue boundaries, not individual
    words — while Segments are cues regrouped at sentence boundaries (see
    `_group_into_sentences`) so the LLM judges whole sentences. A cue holding
    several ``<v>`` voices becomes one Word per voice.

    ``max_duration_s`` is the recording's real length: cues starting at or
    after it are dropped and cue ends are clamped to it, because a transcript
    can run past a trimmed or partially downloaded recording, and a cue past
    the end of the media would become an EDL range with nothing to cut.
    """
    cues = _read_cues(text)
    for cue in cues:
        cue.runs = _voice_runs(cue.lines)

    # Pass 1: how often each candidate prefix opens a cue, so a participant
    # whose display name doesn't look like a name ("jdoe", a dial-in number)
    # is still recognised once they've spoken twice.
    prefix_counts: Counter[str] = Counter()
    for cue in cues:
        if cue.runs and cue.runs[0][0] is None:
            candidate = _prefix_candidate(cue.runs[0][1])
            if candidate:
                prefix_counts[candidate[0]] += 1

    words: list[Word] = []
    dropped = clamped = 0
    for cue in cues:
        start, end = cue.start, cue.end
        if max_duration_s is not None:
            if start >= max_duration_s:
                dropped += 1
                continue
            if end > max_duration_s:
                end = max_duration_s
                clamped += 1

        resolved: list[tuple[str, str]] = []
        for i, (voice, run_text) in enumerate(cue.runs or []):
            if voice is not None:
                resolved.append((voice, run_text))
            elif i == 0:
                resolved.append(_resolve_speaker(run_text, prefix_counts, default_speaker))
            else:
                resolved.append((default_speaker, run_text))
        if not resolved:
            continue

        spans = _split_span(start, end, [t for _, t in resolved])
        for (speaker, run_text), (run_start, run_end) in zip(resolved, spans, strict=True):
            words.append(Word(word=run_text, start=run_start, end=run_end, speaker=speaker))

    if dropped or clamped:
        logger.info(
            "transcript runs past the %.1fs recording: dropped %d cue(s), clamped %d", max_duration_s, dropped, clamped
        )

    words.sort(key=lambda w: w.start)
    segments = _group_into_sentences(words)
    return words, segments


def load_uploaded_transcript(
    path: Path, max_duration_s: float | None = None
) -> tuple[list[Word], list[Segment]] | None:
    """Parse a .vtt/.srt transcript (Zoom's audio transcript or a user upload).

    Returns None (never raises) on any read/parse failure or an empty result
    so callers fall back to WhisperX. ``max_duration_s`` drops/clamps cues
    past the end of the recording (see parse_subtitle_text).
    """
    try:
        # utf-8-sig strips a BOM; errors="replace" keeps a stray non-UTF-8
        # byte from costing us the whole transcript.
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        words, segments = parse_subtitle_text(text, max_duration_s=max_duration_s)
    except Exception as exc:  # noqa: BLE001 — malformed file falls back, never fails the job
        logger.warning("failed to parse uploaded transcript %s: %s — will transcribe ourselves", path, exc)
        return None

    if not words:
        return None

    logger.info("using uploaded transcript %s (%d cues) — skipping WhisperX", path, len(words))
    return words, segments


# ------------------------------------------------------------------ YouTube captions


def _parse_json3(data: dict, default_speaker: str = DEFAULT_SPEAKER) -> tuple[list[Word], list[Segment]]:
    """Parse YouTube's json3 caption format, where each line appears once
    (unlike the rolling/karaoke-style VTT export of the same track).

    An auto-caption event lasts as long as its line stays on screen, which
    runs well into the next line's speech, so each event's end is clipped to
    where the next one starts — exactly how YouTube's own VTT export of the
    track times it. Left overlapping, a kept sentence's range swallows the
    start of a removed one after it: the same keep-erases-remove EDL failure
    _group_into_sentences guards against, measured at ~17 minutes of overlap
    across 364 sentence pairs on a real 98-minute meeting.
    """
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
    for word, following in pairwise(words):
        if word.start < following.start < word.end:
            word.end = following.start
    segments = _group_into_sentences(words)
    return words, segments


def _parse_youtube_vtt(text: str) -> tuple[list[Word], list[Segment]]:
    """Parse a YouTube VTT caption track (the fallback when there's no json3).

    Auto-caption VTT is "rolling": each cue shows the previous line again
    above the new one, and a 10ms cue in between shows the finished line on
    its own, so read naively the LLM would see every line two or three times.
    A line identical to the last line kept is dropped, but only in the two
    places the rolling format puts a repeat — the first line of a multi-line
    cue, or a transition cue — so a line genuinely said twice ("No." / "No.")
    survives; a cue left with nothing new is skipped. Creator-uploaded VTT
    isn't rolling and passes through unchanged.

    Unlike parse_subtitle_text there's no speaker detection: captions carry no
    speakers, and a caption that opens "Note: ..." or "JOHN: ..." must stay
    text under DEFAULT_SPEAKER, exactly as it does on the json3 path.
    """
    words: list[Word] = []
    last_line: str | None = None
    for cue in _read_cues(text):
        lines = [line for line in (_clean_text(raw) for raw in cue.lines) if line]
        is_transition = cue.end - cue.start < _ROLLING_TRANSITION_MAX_SECONDS
        new_lines: list[str] = []
        for i, line in enumerate(lines):
            rolling_slot = is_transition or (i == 0 and len(lines) > 1)
            if rolling_slot and line == last_line:
                continue
            new_lines.append(line)
            last_line = line
        if new_lines:
            words.append(Word(word=" ".join(new_lines), start=cue.start, end=cue.end, speaker=DEFAULT_SPEAKER))

    words.sort(key=lambda w: w.start)
    segments = _group_into_sentences(words)
    return words, segments


def _is_original_track(entry: dict) -> bool:
    """False for an HLS entry (its URL returns an m3u8 playlist, not captions)
    and for a machine translation (YouTube's ``tlang`` query parameter)."""
    if str(entry.get("protocol", "")).startswith("m3u8"):
        return False
    return "tlang" not in parse_qs(urlparse(str(entry.get("url", ""))).query)


def _pick_track(manual: dict, automatic: dict) -> dict | None:
    """Prefer creator-uploaded captions over auto-generated ones, and prefer the
    json3 format (clean timing) over vtt (rolling/karaoke for auto-captions).

    Machine translations are never picked. yt-dlp files a translation under
    its target language, so when YouTube ran ASR in several languages "en"
    can list Arabic ASR translated to English ahead of the real English ASR —
    seen on a real English team meeting, where the translated track was also
    the one YouTube answered with HTTP 429. A video with no original English
    track falls back to WhisperX rather than cutting on a translation.
    """
    for source in (manual, automatic):
        for lang, entries in source.items():
            if lang == "en" or lang.startswith(("en-", "en_")):
                for ext in ("json3", "vtt"):
                    for entry in entries:
                        if entry.get("ext") == ext and _is_original_track(entry):
                            return entry
    return None


def _download_track(ydl, info: dict, track: dict) -> bytes:
    """Fetch one caption track through yt-dlp's own downloader, the way its
    --write-subs does, instead of a bare urlopen. yt-dlp marks every YouTube
    caption URL as wanting browser impersonation; its downloader applies that
    (when curl_cffi is installed) plus the extractor's headers, and picks up
    whatever else yt-dlp learns to send for captions in later releases."""
    with tempfile.TemporaryDirectory(prefix="hc-captions-") as tmp:
        path = Path(tmp) / f"captions.{track.get('ext') or 'txt'}"
        request = {**track}
        request.setdefault("http_headers", info.get("http_headers"))
        ydl.dl(str(path), request, subtitle=True)
        return path.read_bytes()


def fetch_youtube_transcript(url: str) -> tuple[list[Word], list[Segment]] | None:
    """Try to reuse YouTube's own captions instead of running WhisperX.

    Never raises — returns None on any failure or missing caption track so
    callers can fall back to self-hosted transcription. Retries once on a
    transient network error (see _YOUTUBE_FETCH_ATTEMPTS) before giving up.
    """
    import yt_dlp

    # Imported here, like yt_dlp, so importing transcribe never loads the
    # ingest package, which could otherwise become a circular import.
    from ingest.youtube import ydl_base_opts

    ydl_opts = {
        **ydl_base_opts(),
        "skip_download": True,
        "socket_timeout": _YOUTUBE_FETCH_SOCKET_TIMEOUT_SECONDS,
    }

    result = None
    ext = None
    last_exc = None
    for attempt in range(1, _YOUTUBE_FETCH_ATTEMPTS + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                track = _pick_track(info.get("subtitles") or {}, info.get("automatic_captions") or {})
                if track is None:
                    logger.info("no usable English caption track for %s — will transcribe ourselves", url)
                    return None

                raw = _download_track(ydl, info, track)
                ext = track.get("ext")
                if ext == "json3":
                    result = _parse_json3(json.loads(raw))
                else:
                    result = _parse_youtube_vtt(raw.decode("utf-8", errors="replace"))
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

    logger.info(
        "using YouTube's own %s captions for %s (%d cues, %d sentences) — skipping WhisperX",
        ext,
        url,
        len(words),
        len(segments),
    )
    return words, segments
