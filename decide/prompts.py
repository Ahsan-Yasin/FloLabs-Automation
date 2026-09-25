"""Prompts and JSON schemas for the v2 decide stage (plan D4, D10).

Three kinds of call:
1. cleanup + scoring, one per chunk of ~30 sentences: keep/remove for the
   cleaned meeting and, independently, a 0-10 highlight score;
2. one global re-rank over the best candidate moments: calibrated scores,
   titles, hooks, whether a moment works as a stand-alone short, and a window
   that includes the setup/punchline;
3. chapters over the cleaned transcript.

The keep/remove rules are the light-cleanup prompt that was stress-tested on
the real 98-minute meeting (see SESSION_HANDOFF.md) — change them carefully.
"""

from __future__ import annotations

from typing import get_args

from core.models import HighlightCategory, RemovalCategory

REMOVAL_CATEGORIES = list(get_args(RemovalCategory))
HIGHLIGHT_CATEGORIES = list(get_args(HighlightCategory))

CLEANUP_RULES = """You are lightly cleaning up a meeting recording transcript. The goal \
is to keep only what is actually on the meeting's agenda — the substance people came \
for — and cut everything else, while staying a light trim, not a highlight reel.

For EVERY segment, decide "keep" or "remove":

Remove — anything that is not the actual agenda content, even if it is on-topic-adjacent \
and nobody is interrupting:
- Crosstalk/interruptions/simultaneous speech that carries no distinguishable content on \
its own (filler like "yeah", "mhm", "sorry go ahead", false starts talked over).
- Greetings, small talk, and pleasantries ("how was your weekend", "good morning everyone", \
"can you hear me", "let me turn my camera on").
- Round-robin self-introductions and "welcome to the team" ceremony — "please introduce \
yourself" / "hi, I'm X, I study Y" — even though it's on-topic, it is not agenda content.
- Waiting-for-people-to-join dead air, technical housekeeping (audio/video troubleshooting), \
and meta-commentary about the meeting itself ("let's wait for everyone", "can we start now").
- Any tangent that isn't the thing the meeting was called to discuss.

Keep — the actual agenda:
- Anything that makes a point, answers a substantive question, asks a substantive question, \
states a decision, or would be missed by someone who only wants the meeting's real content. \
This includes status updates, blockers, and task assignments even when they're introduced by \
someone being called on by name ("can you give us an update?") — being called on is not the \
same as being asked to introduce yourself; judge the answer by whether it's substance or filler.
- Overlap alone is NOT a reason to remove."""

CLEANUP_SCORING_TEMPLATE = CLEANUP_RULES + """

You will be given a JSON list of sentence-level segments in chronological order. Each has:
- index: the segment's id — copy it exactly into your answer
- speaker: a speaker label (may be wrong — do not use it to judge importance)
- start, end: seconds
- text: what was said
- overlap_candidate: true if timing suggests this segment overlaps an adjacent one

For every segment also return:
- removal_category: why it is removed, one of {removal_categories}; use "none" when kept.
- highlight_score: an integer 0-10 for how much this segment deserves a place in a short \
3-5 minute highlights reel of the whole meeting. Highlight-worthy means: {highlights_criteria}. \
Score independently of keep/remove (a removed joke during small talk can still score high). \
Use the full range: 0-2 routine or filler, 3-5 useful but ordinary, 6-7 notable, 8-10 one of \
the best moments of the meeting. Most segments should score 0-3.
- highlight_category: the best-fitting one of {highlight_categories}; "none" when the score is \
below 4.
- reason: a few words.

Return {{"judgments": [...]}} with exactly one judgment per input segment, in the same order."""

RERANK_TEMPLATE = """You are choosing highlight moments from a whole meeting. You get candidate \
moments that an earlier pass scored per sentence, each with a little context before and after \
(context segments have their own index). Highlight-worthy means: {highlights_criteria}.

For each candidate return:
- id: copy it.
- score: 0-10, calibrated ACROSS all candidates (the best moments of this meeting get 8-10; \
be strict — at most about a quarter of candidates should be 7 or higher).
- category: one of {highlight_categories}.
- title: at most 60 characters, specific ("Switching the session store to Redis"), no quotes.
- hook: one sentence (at most 140 characters) saying why it is worth watching.
- first_index, last_index: the segment range that makes the moment complete and understandable \
on its own — include the setup a viewer needs and the payoff; you may use the context segments \
shown; keep it tight.
- short_worthy: true only if, on its own as a 20-60 second vertical clip, it fits: \
{shorts_criteria}.

Return {{"moments": [...]}} with one entry per candidate id."""

CHAPTERS_TEMPLATE = """You are writing YouTube chapters for an edited meeting recording. You get \
the transcript as numbered blocks in order, each with its start time. Choose where topics change.

Rules:
- The first chapter starts at block 0.
- Between {min_chapters} and {max_chapters} chapters; a new chapter only where the topic really \
changes; chapters at least {min_gap_s} seconds apart.
- Titles: at most 60 characters, specific and descriptive, no timestamps, no quotes, no "<" or ">".

Return {{"chapters": [{{"block": <block number>, "title": "..."}}, ...]}} in order."""


def judgment_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "judgments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "decision": {"type": "string", "enum": ["keep", "remove"]},
                        "reason": {"type": "string"},
                        "removal_category": {"type": "string", "enum": REMOVAL_CATEGORIES},
                        "highlight_score": {"type": "integer", "minimum": 0, "maximum": 10},
                        "highlight_category": {"type": "string", "enum": HIGHLIGHT_CATEGORIES},
                    },
                    "required": ["index", "decision", "reason", "removal_category", "highlight_score",
                                 "highlight_category"],
                },
            }
        },
        "required": ["judgments"],
    }


def rerank_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "moments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "score": {"type": "integer", "minimum": 0, "maximum": 10},
                        "category": {"type": "string", "enum": HIGHLIGHT_CATEGORIES},
                        "title": {"type": "string"},
                        "hook": {"type": "string"},
                        "first_index": {"type": "integer"},
                        "last_index": {"type": "integer"},
                        "short_worthy": {"type": "boolean"},
                    },
                    "required": ["id", "score", "category", "title", "hook", "first_index", "last_index",
                                 "short_worthy"],
                },
            }
        },
        "required": ["moments"],
    }


def chapters_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "chapters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"block": {"type": "integer"}, "title": {"type": "string"}},
                    "required": ["block", "title"],
                },
            }
        },
        "required": ["chapters"],
    }


def cleanup_scoring_prompt(highlights_criteria: str) -> str:
    return CLEANUP_SCORING_TEMPLATE.format(
        removal_categories=", ".join(f'"{c}"' for c in REMOVAL_CATEGORIES),
        highlight_categories=", ".join(f'"{c}"' for c in HIGHLIGHT_CATEGORIES),
        highlights_criteria=highlights_criteria.strip(),
    )


def rerank_prompt(highlights_criteria: str, shorts_criteria: str) -> str:
    return RERANK_TEMPLATE.format(
        highlights_criteria=highlights_criteria.strip(),
        shorts_criteria=shorts_criteria.strip(),
        highlight_categories=", ".join(f'"{c}"' for c in HIGHLIGHT_CATEGORIES if c != "none"),
    )


def chapters_prompt(min_chapters: int, max_chapters: int, min_gap_s: int = 10) -> str:
    return CHAPTERS_TEMPLATE.format(min_chapters=min_chapters, max_chapters=max_chapters, min_gap_s=min_gap_s)
