import json
import re
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.models import Segment, Word

logger = get_logger(__name__)

_TIMESTAMP = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})")
_TAG = re.compile(r"<[^>]+>")
_VOICE_TAG = re.compile(r"<v(?:\.[\w-]+)*\s+([^>]+)>")
_SPEAKER_PREFIX = re.compile(r"^([A-Za-z][\w .'\-]{0,40}):\s+(.*)$")

DEFAULT_SPEAKER = "SPEAKER"


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_subtitle_text(text: str, default_speaker: str = DEFAULT_SPEAKER) -> tuple[list[Word], list[Segment]]:
    """Parse a WebVTT or SRT file into cue-level Words/Segments.

    Used for user-supplied transcripts (e.g. exported from Zoom), which are
    clean single-pass cues — unlike YouTube's rolling auto-caption VTT, so no
    dedup pass is needed here. Each cue becomes one pseudo-"word" (its full
    text) and one Segment; there's no finer timing than the subtitle track
    provides, so downstream cuts snap to cue boundaries, not individual words.
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
    segments = [Segment(speaker=w.speaker, start=w.start, end=w.end, text=w.word) for w in words]
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
    segments = [Segment(speaker=w.speaker, start=w.start, end=w.end, text=w.word) for w in words]
    return words, segments


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
    callers can fall back to self-hosted transcription.
    """
    import yt_dlp

    settings = get_settings()
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    ffmpeg_path = Path(settings.ffmpeg_bin)
    if ffmpeg_path.is_file():
        ydl_opts["ffmpeg_location"] = str(ffmpeg_path.parent)

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
                words, segments = _parse_json3(json.loads(raw))
            else:
                words, segments = parse_subtitle_text(raw.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 — yt-dlp/network/parse errors all fall back, never fail the job
        logger.warning("YouTube caption fetch failed for %s: %s — will transcribe ourselves", url, exc)
        return None

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
