"""Captions for the vertical shorts (plan D7).

Zoom transcripts are sentence-level (one "word" per sentence here), far too
long for a phone screen, so each sentence is split into words whose times are
spread over the sentence in proportion to their length, then regrouped into
short cues (a few words, never across a sentence end or a pause). Cue times
are on the clip's own timeline (source time − clip start).

Two files come out of one cue list: an .srt shipped next to the short (for
platforms that take a caption upload) and an .ass burned into the video with
libass — ASS gives the exact style (size, outline, position, wrapping) without
fragile force_style strings, plus the moment's title at the top.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.models import Word

SENTENCE_END = (".", "!", "?", "…")


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def caption_cues(
    items: list[Word],
    clip_start: float,
    clip_end: float,
    *,
    max_chars: int = 30,
    max_words: int = 6,
    max_pause_s: float = 0.8,
    bridge_s: float = 0.5,
) -> list[Cue]:
    """Short caption cues for [clip_start, clip_end) (source seconds) on the
    clip timeline (0 = clip start). A word belongs to the clip if its
    estimated midpoint is inside it."""
    tokens: list[tuple[float, float, str]] = []
    cursor = float("-inf")
    for item in sorted(items, key=lambda w: w.start):
        if item.end <= clip_start or item.start >= clip_end:
            continue
        words = item.word.split()
        if not words:
            continue
        # a line never starts before the previous one ends: rolling/overlapping
        # transcript cues otherwise interleave their words and put two
        # captions on screen at once
        begin = max(item.start, cursor)
        end = max(item.end, begin + 0.25 * len(words))
        cursor = end
        weight = sum(len(w) + 1 for w in words)
        t = begin
        for w in words:
            d = (end - begin) * (len(w) + 1) / weight
            if clip_start <= t + d / 2 < clip_end:
                tokens.append((max(t, clip_start), min(t + d, clip_end), w))
            t += d

    groups: list[list[tuple[float, float, str]]] = []
    current: list[tuple[float, float, str]] = []
    for tok in tokens:
        if current:
            text = " ".join(t for _, _, t in current)
            if (len(text) + 1 + len(tok[2]) > max_chars or len(current) >= max_words
                    or tok[0] - current[-1][1] > max_pause_s):
                groups.append(current)
                current = []
        current.append(tok)
        if tok[2].endswith(SENTENCE_END):
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    cues = [Cue(g[0][0] - clip_start, g[-1][1] - clip_start, " ".join(t for _, _, t in g)) for g in groups]
    # close small gaps so captions don't flicker off between cues
    out: list[Cue] = []
    for i, cue in enumerate(cues):
        end = cue.end
        if i + 1 < len(cues) and 0 <= cues[i + 1].start - end <= bridge_s:
            end = cues[i + 1].start
        if end > cue.start:
            out.append(Cue(round(cue.start, 3), round(end, 3), cue.text))
    return out


def _srt_time(t: float) -> str:
    ms = max(0, round(t * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_text(cues: list[Cue]) -> str:
    blocks = [f"{i}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{c.text}\n" for i, c in enumerate(cues, 1)]
    return "\n".join(blocks)


def _ass_time(t: float) -> str:
    cs = max(0, math.floor(t * 100 + 0.5))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    # "{...}" starts an override block and a backslash starts a tag
    return " ".join(text.replace("\\", "/").replace("{", "(").replace("}", ")").split())


def ass_document(cues: list[Cue], *, title: str, duration_s: float, font_family: str, width: int,
                 height: int) -> str:
    """Captions near the bottom, the title at the top (whole clip), sized for
    width x height (1080x1920 by default)."""
    k = height / 1920
    caption_size, title_size = round(76 * k), round(62 * k)
    margin_x = round(90 * width / 1080)
    fields = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
              "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
              "Alignment, MarginL, MarginR, MarginV, Encoding")
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        fields,
        (f"Style: Caption,{font_family},{caption_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,"
         f"100,0,0,1,{max(1, round(5 * k))},0,2,{margin_x},{margin_x},{round(430 * k)},1"),
        (f"Style: Title,{font_family},{title_size},&H0000E5FF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,"
         f"0,0,1,{max(1, round(4 * k))},0,8,{margin_x},{margin_x},{round(300 * k)},1"),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if title.strip():
        lines.append(f"Dialogue: 0,{_ass_time(0)},{_ass_time(duration_s)},Title,,0,0,0,,{_ass_escape(title)}")
    for cue in cues:
        lines.append(f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Caption,,0,0,0,,{_ass_escape(cue.text)}")
    return "\n".join(lines) + "\n"
