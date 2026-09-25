"""YouTube chapters (plan D10).

1. `generate_chapters`: the LLM picks topic starts from ~50 s blocks of the
   CLEANED transcript (block starts are sentence starts, cleaned time).
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
import json
import math
import re
from dataclasses import dataclass, field

from core.config import get_settings
from core.logging import get_logger
from core.models import Chapter, Word

from .gemini_client import DecisionError, GeminiCaller, make_caller
from .prompts import chapters_prompt, chapters_schema

logger = get_logger(__name__)

MIN_CHAPTERS = 3
MIN_CHAPTER_S = 10.0
MAX_DESCRIPTION_BYTES = 5000
TITLE_LIMIT = 60
BLOCK_TEXT_LIMIT = 1200


@dataclass
class ChapterResult:
    chapters: list[Chapter]
    ok: bool
    problems: list[str] = field(default_factory=list)


def build_blocks(clean_words: list[Word], block_s: float = 50.0) -> list[dict]:
    """Consecutive sentences grouped into blocks of about `block_s` seconds;
    every block starts at a sentence start (cleaned time)."""
    blocks: list[dict] = []
    current: list[Word] = []

    def flush():
        if current:
            text = " ".join(w.word for w in current)
            blocks.append({
                "block": len(blocks),
                "start": round(current[0].start, 2),
                "text": text[:BLOCK_TEXT_LIMIT],
            })

    for w in clean_words:
        if current and w.start - current[0].start >= block_s:
            flush()
            current = []
        current.append(w)
    flush()
    return blocks


def chapter_count_range(duration_s: float) -> tuple[int, int]:
    """About one chapter per 4-8 minutes, between 3 and 15."""
    hi = max(MIN_CHAPTERS, min(15, math.ceil(duration_s / 240)))
    lo = max(MIN_CHAPTERS, min(hi, math.floor(duration_s / 480)))
    return lo, hi


def clean_title(title: str) -> str:
    text = re.sub(r"[<>\r\n\t]+", " ", str(title or ""))
    text = re.sub(r"^[\s\-–—:|•\d.]+", "", text)  # no leading timestamp/bullet
    text = re.sub(r"\s{2,}", " ", text).strip().strip('"').strip()
    if len(text) > TITLE_LIMIT:
        text = text[: TITLE_LIMIT - 1].rstrip() + "…"
    return text


def generate_chapters(
    clean_words: list[Word], duration_s: float, *, caller: GeminiCaller | None = None
) -> ChapterResult:
    """Topic chapters on the cleaned timeline. One retry with the problems
    spelled out; never raises for bad output (chapters are optional)."""
    settings = get_settings()
    blocks = build_blocks(clean_words, settings.chapter_block_s)
    if duration_s < MIN_CHAPTERS * MIN_CHAPTER_S + MIN_CHAPTER_S or len(blocks) < MIN_CHAPTERS:
        return ChapterResult([], False, ["video too short for chapters"])
    lo, hi = chapter_count_range(duration_s)
    caller = caller or make_caller()
    system = chapters_prompt(lo, hi, int(MIN_CHAPTER_S))
    payload = json.dumps(
        [{"block": b["block"], "start": _clock(b["start"]), "text": b["text"]} for b in blocks], ensure_ascii=False
    )
    note = ""
    problems: list[str] = []
    for _attempt in range(2):
        try:
            response = caller.generate(system, f"{note}\n\n{payload}" if note else payload, chapters_schema())
            data = json.loads(response.text)
            items = data.get("chapters") if isinstance(data, dict) else data
            chapters = _to_chapters(items, blocks)
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError, KeyError, DecisionError) as exc:
            problems = [f"unusable answer: {exc}"]
            note = "Your previous answer was not valid JSON matching the schema. Try again."
            continue
        _, problems = finalize_chapters(chapters, final_duration_s=duration_s, reel_s=0.0)
        if not problems:
            return ChapterResult(chapters, True)
        note = "Your previous chapters were rejected: " + "; ".join(problems) + ". Fix these and answer again."
        logger.warning("chapters rejected (%s), retrying once", "; ".join(problems))
    return ChapterResult([], False, problems)


def _to_chapters(items, blocks: list[dict]) -> list[Chapter]:
    if not isinstance(items, list):
        raise TypeError("no chapters list")
    seen: set[int] = set()
    chapters = []
    for item in items:
        b = int(item["block"])
        if not 0 <= b < len(blocks) or b in seen:
            continue
        title = clean_title(item.get("title", ""))
        if not title:
            continue
        seen.add(b)
        chapters.append(Chapter(start=float(blocks[b]["start"]), title=title))
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


def _clock(seconds: float, hours: bool = False) -> str:
    total = max(0, math.floor(seconds + 1e-6))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h or hours:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_chapters(entries: list[tuple[int, str]], final_duration_s: float) -> str:
    """'00:00 Title' lines; H:MM:SS once past an hour."""
    return "\n".join(f"{_clock(t)} {title}" for t, title in entries) + ("\n" if entries else "")
