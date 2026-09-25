"""YouTube chapters (plan D10).

1. `generate_chapters`: the LLM picks the sentence that introduces each topic
   from the CLEANED transcript (numbered lines with their cleaned time).
   Sentence-level picks put a chapter exactly where the new topic starts; the
   first version used ~50 s blocks and started 5 of 12 chapters 20-40 s early.
2. `finalize_chapters`: map them onto the final video — with a highlights reel
   in front, shift by the reel's measured length and prepend "00:00
   Highlights"; without one, the first topic starts at 00:00 — then drop
   entries that would break YouTube's rules and validate what is left.
3. `format_chapters`: the text to paste into the video description.

YouTube only turns a description into chapters when: the first timestamp is
00:00, there are at least 3, each is at least 10 s long, they ascend, and the
description stays under 5000 bytes. Titles must not contain < or >.
"""

from __future__ import annotations

import itertools
import math
import re
import statistics
from dataclasses import dataclass, field

from core.logging import get_logger
from core.models import Chapter, Word

from .gemini_client import DecisionError, GeminiCaller, LazyCaller, make_caller
from .prompts import chapters_prompt

logger = get_logger(__name__)

MIN_CHAPTERS = 3
MIN_CHAPTER_S = 10.0
MAX_DESCRIPTION_BYTES = 5000
TITLE_LIMIT = 60
LINE_TEXT_LIMIT = 220  # characters of each sentence sent (topic changes show early)
MAX_CHAPTER_S = 600.0  # "no chapter longer than about 10 minutes"
LONG_CHAPTER_FACTOR = 2.5
MIN_TOPIC_S = 45.0  # shorter "chapters" are usually a mis-numbered line


@dataclass
class ChapterResult:
    chapters: list[Chapter]
    ok: bool
    problems: list[str] = field(default_factory=list)


def chapter_count_range(duration_s: float) -> tuple[int, int]:
    """About one chapter per 4-8 minutes, between 3 and 15."""
    hi = max(MIN_CHAPTERS, min(15, math.ceil(duration_s / 240)))
    lo = max(MIN_CHAPTERS, min(hi, math.floor(duration_s / 480)))
    return lo, hi


def clean_title(title: str) -> str:
    text = re.sub(r"[<>\r\n\t]+", " ", str(title or ""))
    # strip only a real leading timestamp, list number or bullet — never the
    # start of a title like "3D viewer demo"
    text = re.sub(r"^\s*(?:(?:\d{1,2}:)?\d{1,2}:\d{2}\s*[-–—:|•]?\s*|(?:\d+[.)]|[-–—•|])\s+)", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip().strip('"').strip()
    if len(text) > TITLE_LIMIT:
        text = text[: TITLE_LIMIT - 1].rstrip() + "…"
    return text


def _clock(seconds: float, hours: bool = False) -> str:
    total = max(0, math.floor(seconds + 1e-6))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h or hours:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def chapter_lines(clean_words: list[Word]) -> str:
    return "\n".join(f"[{i} {_clock(w.start)}] {w.word[:LINE_TEXT_LIMIT]}" for i, w in enumerate(clean_words))


def generate_chapters(
    clean_words: list[Word], duration_s: float, *, caller: GeminiCaller | LazyCaller | None = None
) -> ChapterResult:
    """Topic chapters on the cleaned timeline. One retry with the problems
    spelled out; never raises for bad output (chapters are optional)."""
    if duration_s < (MIN_CHAPTERS + 1) * MIN_CHAPTER_S or len(clean_words) < MIN_CHAPTERS:
        return ChapterResult([], False, ["video too short for chapters"])
    lo, hi = chapter_count_range(duration_s)
    max_min = max(3, round(max(MAX_CHAPTER_S, 0.15 * duration_s) / 60))
    caller = caller or make_caller()
    system = chapters_prompt(lo, hi, int(MIN_CHAPTER_S), max_min)
    payload = chapter_lines(clean_words)
    note = ""
    problems: list[str] = []
    for attempt in range(2):
        try:
            response = caller.generate(system, f"{note}\n\n{payload}" if note else payload)
            chapters = _to_chapters(parse_chapter_lines(response.text), clean_words)
        except (ValueError, TypeError, AttributeError, KeyError, DecisionError) as exc:
            problems = [f"unusable answer: {exc}"]
            note = 'Your previous answer was not in the form "<line number>|<title>", one per line. Try again.'
            continue
        _, problems = finalize_chapters(chapters, final_duration_s=duration_s, reel_s=0.0)
        long_ones = _too_long(chapters, duration_s)
        if not problems and not long_ones:
            return ChapterResult(chapters, True)
        if not problems and attempt == 1:
            return ChapterResult(chapters, True)  # long chapters are a quality issue, not invalid
        problems = problems + long_ones
        note = "Your previous chapters were rejected: " + "; ".join(problems) + ". Fix these and answer again."
        logger.warning("chapters rejected (%s), retrying once", "; ".join(problems))
    return ChapterResult([], False, problems)


def _too_long(chapters: list[Chapter], duration_s: float) -> list[str]:
    """Quality problems worth one retry: a chapter far longer than the rest
    (several topics lumped together), or one too short to be a topic (usually
    a mis-numbered line). Not invalid — the retry's answer is used either way."""
    if len(chapters) < 2:
        return []
    bounds = [c.start for c in chapters] + [duration_s]
    lengths = [b - a for a, b in itertools.pairwise(bounds)]
    limit = max(MAX_CHAPTER_S, LONG_CHAPTER_FACTOR * statistics.median(lengths))
    problems = [
        f"the chapter '{c.title}' at {_clock(c.start)} runs {length / 60:.0f} minutes — split it where the "
        "topic, team or item changes"
        for c, length in zip(chapters, lengths, strict=True) if length > limit
    ]
    problems += [
        f"the chapter '{c.title}' at {_clock(c.start)} lasts only {length:.0f} seconds — start each chapter at "
        "the line where its topic really begins"
        for c, length in zip(chapters, lengths, strict=True) if length < MIN_TOPIC_S
    ]
    return problems


_CHAPTER_LINE = re.compile(r"^\[?(\d+)(?:\s+[\d:]+)?\]?\s*\|\s*(.+)$")


def parse_chapter_lines(raw: str) -> list[dict]:
    """'<line number>|<title>' lines; other lines are ignored."""
    items = []
    for line in (raw or "").splitlines():
        m = _CHAPTER_LINE.match(line.strip())
        if m:
            items.append({"i": int(m.group(1)), "t": m.group(2).strip()})
    if not items:
        raise ValueError("no chapter lines in the answer")
    return items


def _to_chapters(items, clean_words: list[Word]) -> list[Chapter]:
    if not isinstance(items, list):
        raise TypeError("no chapters list")
    seen: set[int] = set()
    chapters = []
    for item in items:
        i = int(item["i"])
        if not 0 <= i < len(clean_words) or i in seen:
            continue
        title = clean_title(item.get("t", ""))
        if not title:
            continue
        seen.add(i)
        chapters.append(Chapter(start=float(clean_words[i].start), title=title))
    chapters.sort(key=lambda c: c.start)
    return chapters


def finalize_chapters(
    chapters: list[Chapter], *, final_duration_s: float, reel_s: float = 0.0
) -> tuple[list[tuple[int, str]], list[str]]:
    """Place chapters on the final video and validate. Returns (entries as
    (whole seconds, title), problems); entries is empty whenever problems is
    not — an invalid list must not be published, YouTube would ignore it."""
    body = [(c.start + reel_s, clean_title(c.title)) for c in sorted(chapters, key=lambda c: c.start)]
    body = [(t, title) for t, title in body if title]
    if reel_s > 0:
        # the first topic starts where the meeting starts, else "Highlights"
        # would also cover the opening of the meeting
        if body:
            body[0] = (reel_s, body[0][1])
        entries = [(0.0, "Highlights")] + body
    else:
        entries = body
        if entries:
            entries[0] = (0.0, entries[0][1])
    kept: list[tuple[int, str]] = []
    for t, title in entries:
        sec = math.floor(t + 1e-6)
        if kept and sec - kept[-1][0] < MIN_CHAPTER_S:
            continue  # too close to the previous chapter: keep the earlier one
        if kept and sec > final_duration_s - MIN_CHAPTER_S:
            continue  # the last chapter must be at least 10 s long
        kept.append((sec, title))
    problems = validate_chapter_entries(kept, final_duration_s)
    return (kept if not problems else []), problems


def validate_chapter_entries(entries: list[tuple[int, str]], final_duration_s: float) -> list[str]:
    problems = []
    if len(entries) < MIN_CHAPTERS:
        problems.append(f"only {len(entries)} chapters, YouTube needs at least {MIN_CHAPTERS}")
    if entries and entries[0][0] != 0:
        problems.append("the first chapter must start at 00:00")
    for (a, _), (b, _) in itertools.pairwise(entries):
        if b - a < MIN_CHAPTER_S:
            problems.append(f"chapters at {a}s and {b}s are less than {MIN_CHAPTER_S:.0f}s apart")
            break
    if entries and final_duration_s - entries[-1][0] < MIN_CHAPTER_S:
        problems.append("the last chapter is shorter than 10 s")
    for _, title in entries:
        if not title or len(title) > TITLE_LIMIT or "<" in title or ">" in title:
            problems.append(f"invalid title {title!r}")
            break
    if entries and len(format_chapters(entries, final_duration_s).encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        problems.append("chapter list is longer than 5000 bytes")
    return problems


def format_chapters(entries: list[tuple[int, str]], final_duration_s: float) -> str:
    """'00:00 Title' lines; H:MM:SS once past an hour."""
    return "\n".join(f"{_clock(t)} {title}" for t, title in entries) + ("\n" if entries else "")
