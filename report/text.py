"""Plain-text and JSON transcripts shipped in the bundle (plan D11).

* transcript_clean: what is said in final.mp4, with final.mp4 times (the
  cleaned meeting starts after the intro, the highlights reel and the title
  card).
* transcript_removed: every cut, with its SOURCE time range (the original
  recording's clock — the same times the labels in removed.mp4 show), the
  reason, whether it is in removed.mp4, and the words that were cut.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from core.models import RemovedRange, RenderManifest, Word
from core.timeline import fmt_clock, parse_rate
from slice.transcript import MIN_OVERLAP_S, merge_spans


@dataclass
class RemovedEntry:
    index: int
    start: float
    end: float
    reason: str
    tier: str
    removed_video_at: float | None  # position in removed.mp4, if shown there
    lines: list[dict] = field(default_factory=list)  # {start, end, speaker, text} (source time)
    silence: bool = False  # nobody was speaking: listed, but not in removed.mp4

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def text(self) -> str:
        return " ".join(line["text"] for line in self.lines)


def removed_entries(removed: list[RemovedRange], words: list[Word], removed_manifest: RenderManifest | None = None,
                    silence: list[tuple[float, float]] | None = None) -> list[RemovedEntry]:
    """Attach the cut words to each removed range (a word belongs to the range
    holding its midpoint, like the clean transcript's remap). A word whose
    midpoint is in a cut pause — a pure-silence range, or one of the
    `silence` cuts (EditDecisionList.silence_cuts; a pause cut can share a
    removed range with a removed sentence) — but which is partly kept was
    still said in the output (slice/transcript.remap_transcript is given the
    same cuts), so it is not listed."""
    starts = [r.start for r in removed]
    silence = merge_spans(silence or [])
    silence_starts = [s for s, _ in silence]
    by_range: dict[int, list[Word]] = {}
    for w in words:
        mid = (w.start + w.end) / 2
        i = bisect.bisect_right(starts, mid) - 1
        if 0 <= i < len(removed) and removed[i].start <= mid < removed[i].end:
            k = bisect.bisect_right(silence_starts, mid) - 1
            in_pause = removed[i].silence or (k >= 0 and mid < silence[k][1])
            if in_pause and _kept_s(w, removed, starts) > MIN_OVERLAP_S:
                continue
            by_range.setdefault(i, []).append(w)

    positions: dict[int, float] = {}
    if removed_manifest is not None:
        fps = parse_rate(removed_manifest.fps)
        for p in removed_manifest.pieces:
            positions[p.src_start_frame] = float(Fraction(p.out_start_frame) / fps)

    entries = []
    for i, r in enumerate(removed):
        at = positions.get(r.start_frame) if r.tier == "video" and r.start_frame is not None else None
        lines = [{"start": round(w.start, 3), "end": round(w.end, 3), "speaker": w.speaker, "text": w.word.strip()}
                 for w in by_range.get(i, []) if w.word.strip()]
        entries.append(RemovedEntry(i + 1, r.start, r.end, r.reason, r.tier, at, lines, r.silence))
    return entries


def _kept_s(w: Word, removed: list[RemovedRange], starts: list[float]) -> float:
    """Seconds of the word that are NOT removed (the removed ranges are the
    exact complement of the kept ones)."""
    cut = 0.0
    i = max(0, bisect.bisect_right(starts, w.start) - 1)
    while i < len(removed) and removed[i].start < w.end:
        cut += max(0.0, min(w.end, removed[i].end) - max(w.start, removed[i].start))
        i += 1
    return (w.end - w.start) - cut


def _who(speaker: str) -> str:
    """"Name: " — but not the placeholder a transcript without speaker names
    gets ("SPEAKER"), which would only repeat on every line."""
    return f"{speaker}: " if speaker and speaker.strip().upper() not in ("SPEAKER", "UNKNOWN") else ""


def _span(a: float, b: float) -> str:
    return f"{fmt_clock(a)}–{fmt_clock(b)}"


def _header(title: str, extra: list[str]) -> list[str]:
    lines = [title, "=" * len(title)]
    lines += extra
    return lines + [""]


def write_removed_transcript(txt_path: Path, json_path: Path, entries: list[RemovedEntry], *, meeting: str,
                             source_duration_s: float) -> None:
    total = sum(e.duration for e in entries)
    # what removed.mp4 really contains (nothing, if it failed or was skipped)
    shown = sum(1 for e in entries if e.removed_video_at is not None)
    where = (f"{shown} cuts of 1 s or more are also in removed.mp4; shorter ones are listed here only."
             if shown else "There is no removed.mp4 for this meeting; every cut is listed here.")
    notes = [
        (f"{len(entries)} cuts, {total / 60:.1f} min of {source_duration_s / 60:.1f} min "
         f"({(100 * total / source_duration_s) if source_duration_s else 0:.0f}%)."),
        "Times are positions in the ORIGINAL recording (the labels in removed.mp4 show the same times).",
        where,
    ]
    silences = [e for e in entries if e.silence]
    if silences:
        notes.append(f"{len(silences)} of the cuts ({sum(e.duration for e in silences):.0f}s) are pauses where no "
                     "one was speaking; there is nothing to see or hear in them, so removed.mp4 leaves them out.")
    header = _header(f"Removed from: {meeting}" if meeting else "Removed parts", notes)
    body: list[str] = []
    for e in entries:
        where = f"  [in removed.mp4 at {fmt_clock(e.removed_video_at)}]" if e.removed_video_at is not None else ""
        body.append(f"[{_span(e.start, e.end)}] ({e.duration:.1f}s) {e.reason or 'removed'}{where}")
        if not e.lines:
            body.append("    (no one speaking)" if e.silence else "    (no speech)")
        speaker = None
        for line in e.lines:
            who = _who(line["speaker"]) if line["speaker"] != speaker else ""
            speaker = line["speaker"] or speaker
            body.append(f"    {who}{line['text']}")
        body.append("")
    txt_path.write_text("\n".join(header + body).rstrip() + "\n", encoding="utf-8")
    json_path.write_text(json.dumps({
        "timeline": "source",
        "meeting": meeting,
        "source_duration_s": round(source_duration_s, 3),
        "removed_s": round(total, 3),
        "silence_cuts": len(silences),
        "silence_s": round(sum(e.duration for e in silences), 3),
        "cuts": [
            {"index": e.index, "start": round(e.start, 3), "end": round(e.end, 3), "duration": round(e.duration, 3),
             "reason": e.reason, "silence": e.silence, "in_removed_video": e.removed_video_at is not None,
             "removed_video_at": None if e.removed_video_at is None else round(e.removed_video_at, 3),
             "lines": e.lines}
            for e in entries
        ],
    }, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_lines(clean_words: list[Word], offset_s: float, reel_words: list[Word] | None = None,
                reel_offset_s: float = 0.0) -> list[dict]:
    """final.mp4 lines: the highlights reel's words, shifted by
    `reel_offset_s` (the intro before it), then the cleaned meeting's,
    shifted by `offset_s`. The owner's intro/outro clips have no transcript."""
    def lines(words: list[Word], offset: float, section: str) -> list[dict]:
        return [{"start": round(w.start + offset, 3), "end": round(w.end + offset, 3), "speaker": w.speaker,
                 "text": w.word.strip(), "section": section} for w in words if w.word.strip()]

    return lines(reel_words or [], reel_offset_s, "highlights") + lines(clean_words, offset_s, "meeting")


_SECTION_TITLES = {"highlights": "HIGHLIGHTS REEL", "meeting": "FULL MEETING"}


def write_clean_transcript(txt_path: Path, json_path: Path, lines: list[dict], *, meeting: str, offset_s: float,
                           final_duration_s: float, intro_s: float = 0.0, outro_s: float = 0.0) -> None:
    notes = [f"Times are positions in final.mp4 ({fmt_clock(final_duration_s)} long)."]
    if offset_s > 0:
        # offset_s = intro + highlights reel + title card
        opening = [part for part, there in ((f"the intro ({intro_s:.0f}s)", intro_s > 0),
                                            ("the highlights reel", offset_s - intro_s > 1e-6)) if there]
        notes.append(f"final.mp4 opens with {' and '.join(opening)}; the full meeting starts at "
                     f"{fmt_clock(offset_s)}.")
    if outro_s > 0:
        notes.append(f"It ends with the outro ({outro_s:.0f}s) after the meeting.")
    header = _header(f"Transcript: {meeting}" if meeting else "Transcript", notes)
    body: list[str] = []
    paragraph: list[str] = []
    speaker, start, section = None, 0.0, None
    sections = {line.get("section") for line in lines}

    def flush():
        if paragraph:
            body.append(f"[{fmt_clock(start)}] {_who(speaker)}{' '.join(paragraph)}")
            body.append("")

    for line in lines:
        if line.get("section") != section:
            flush()
            paragraph, section = [], line.get("section")
            if len(sections) > 1 and section in _SECTION_TITLES:
                body += [f"--- {_SECTION_TITLES[section]} ---", ""]
        # a new paragraph per speaker turn (or after a minute, so long turns stay navigable)
        if line["speaker"] != speaker or line["start"] - start > 60 or not paragraph:
            flush()
            paragraph, speaker, start = [], line["speaker"], line["start"]
        paragraph.append(line["text"])
    flush()
    txt_path.write_text("\n".join(header + body).rstrip() + "\n", encoding="utf-8")
    json_path.write_text(json.dumps({"timeline": "final", "meeting": meeting, "cleaned_starts_at_s": round(offset_s, 3),
                                     "intro_s": round(intro_s, 3), "outro_s": round(outro_s, 3),
                                     "lines": lines}, indent=2, ensure_ascii=False), encoding="utf-8")
