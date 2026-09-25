"""Prompts and JSON schemas for the v2 decide stage (plan D4, D10).

Three kinds of call:
1. cleanup + scoring, one per chunk of ~60 sentences: keep/remove for the
   cleaned meeting and, independently, a 0-10 highlight score;
2. one global re-rank over the best candidate moments: calibrated scores,
   titles, hooks, whether a moment works as a stand-alone short, and a
   complete window (setup + payoff);
3. chapters over the cleaned transcript.

Token budget matters (the owner asked for it explicitly), so the exchange is
compact: sentences go in as numbered plain-text lines (the speaker name only
when it changes), and answers come back as plain text, one short line per
item ("312 r 0 fill -"), with short category codes that are mapped back to
the full names here. JSON answers were tried first: the model pretty-prints
them, and indentation made up ~80% of the output tokens (37 tokens per
sentence vs ~10 for a line).

The keep/remove rules are the light-cleanup prompt that was stress-tested on
the real 98-minute meeting (see SESSION_HANDOFF.md), plus three rules added
after reviewing the v2 output (transitions/praise, bare answers, sentence
tails) — change them carefully.
"""

from __future__ import annotations

from core.models import Segment

# wire code -> internal category name
REMOVAL_CODES = {
    "fill": "filler",
    "greet": "greeting_small_talk",
    "intro": "ceremony_intros",
    "house": "housekeeping",
    "xtalk": "crosstalk",
    "tang": "tangent",
    "rep": "repetition",
    "dead": "dead_air",
}
HIGHLIGHT_CODES = {
    "fun": "funny",
    "arch": "new_architecture",
    "feat": "new_feature",
    "idea": "concept",
    "dec": "decision",
    "ins": "insight",
}
HIGHLIGHT_TO_CODE = {v: k for k, v in HIGHLIGHT_CODES.items()}

# Human wording used in the removed-parts labels and the report.
REMOVAL_LABELS = {
    "filler": "filler",
    "greeting_small_talk": "greetings / small talk",
    "ceremony_intros": "introductions",
    "housekeeping": "housekeeping",
    "crosstalk": "crosstalk",
    "tangent": "off-topic tangent",
    "repetition": "repetition",
    "dead_air": "dead air",
    "none": "removed",
}

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
- Hand-offs and bare calls to speak that only pass the floor ("let's move to the design \
team", "any updates?", "go ahead", "you can share your screen") and praise with no content \
("looks great", "nice work") — always remove these as housekeeping, wherever they occur. A \
question about the substance itself ("what changed compared to the current version?") is \
content: keep it.
- A question whose only answer is a bare no/nothing ("any blockers?" — "not really"): remove \
the question and the answer.

Keep — the actual agenda:
- Anything that makes a point, answers a substantive question, asks a substantive question, \
states a decision, or would be missed by someone who only wants the meeting's real content. \
This includes status updates, blockers, and task assignments even when they're introduced by \
someone being called on by name ("can you give us an update?") — being called on is not the \
same as being asked to introduce yourself; judge the answer by whether it's substance or filler.
- A segment that only finishes a kept sentence (the previous segment stops mid-sentence) \
takes the same decision as that sentence — never cut a sentence in half.
- Overlap alone is NOT a reason to remove."""

JUDGE_TEMPLATE = CLEANUP_RULES + """

INPUT: numbered transcript lines "[n] Speaker: text" (the speaker is shown only when it \
changes; speaker labels may be wrong). Lines starting with "~" (under "CONTEXT") are only \
there so you can follow the conversation — never include them in your answer. "(overlap)" \
marks timing overlap with a neighbour.

OUTPUT: plain text, exactly one line per JUDGE line, in order, nothing else:
<n> <k|r> <score> <removal code or -> <highlight code or ->
for example "312 r 0 fill -" or "313 k 6 - arch".
- k keep, r remove. The removal code (only when removing) says why: {removal_codes}.
- score 0-10: how much this moment deserves a place in a 3-5 minute highlights reel of the \
whole meeting. Highlight-worthy means: {highlights_criteria}. Judge the whole thought, not \
the single line: every line of a strong explanation, demo, decision or joke gets the score \
of that moment. Anchors: 0-2 logistics, filler, routine status ("I worked on X, still on \
it"); 3-5 useful substance (a concrete update, a real question answered); 6-7 a clear \
decision, an architecture or feature explained or demoed, a surprising fact or number, a \
genuine laugh; 8-10 headline material you would put in a trailer of this meeting. Score \
independently of keep/remove (a removed joke during small talk can still score high).
- highlight code when the score is 3 or more (a joke from 2), the best fit: {highlight_codes}"""

RESCORE_NOTE = (
    "Your previous answer scored every kept line 0. Re-read the scoring anchors: routine status is 0-2, but "
    "useful substance is 3-5 and explanations, demos, decisions and laughs are 6-7. Score again."
)

RERANK_TEMPLATE = """You are choosing highlight moments from a whole meeting. Below are \
candidate moments that an earlier pass scored line by line. Each block starts "#<id> core \
<a>-<b>" followed by numbered lines "[n] Speaker: text"; lines marked "~" are context around \
the core. Highlight-worthy means: {highlights_criteria}.

Compare the candidates WITH EACH OTHER. Answer in plain text with exactly one line for EVERY \
candidate id (even weak ones), nothing else:
<id>|<score>|<code>|<a>|<b>|<y or n>|<title>|<why>
- score 0-10, spread across the candidates: the best ~15% get 8-10, the weakest 1-3
- code: the category, one of {highlight_codes}
- a, b: first and last line number of a clip that works on its own: start where the thought \
starts (include the setup; never start on "which", "and", "so", "the first one"…), end after \
the payoff; usually 15-45 seconds; you may use the "~" lines
- y only if, as a 20-60 second vertical clip on its own, it genuinely is: {shorts_criteria}. A \
useful or well-explained moment is NOT enough on its own. Otherwise n
- title: at most 60 characters, specific ("Moving the session store to Redis"), no "|"
- why: one sentence (at most 140 characters) on why it is worth watching, no "|"
Example: 12|8|arch|402|409|n|Moving the session store to Redis|Cuts API latency from 800 to 120 ms."""

CHAPTERS_TEMPLATE = """You are writing YouTube chapters for an edited meeting recording. You \
get the kept transcript as lines "[n mm:ss] text" in order.

Rules:
- The first chapter starts at line 0.
- Start a new chapter whenever the speaker's topic, team or agenda item changes — each team \
update, demo or review item is its own chapter. Use the line that introduces the new topic.
- Between {min_chapters} and {max_chapters} chapters, at least {min_gap_s} seconds apart; \
no chapter longer than about {max_chapter_min} minutes.
- Each title (at most 60 characters) must describe the WHOLE chapter; no timestamps, no \
quotes, no "<" or ">".

Answer in plain text, one line per chapter in order, nothing else: <line number>|<title>
Example of the format (not of the content):
0|Welcome and agenda
37|Payments team: checkout API migration"""


def _codes(mapping: dict[str, str]) -> str:
    return ", ".join(f'"{k}" {v.replace("_", " ")}' for k, v in mapping.items())


def judge_prompt(highlights_criteria: str) -> str:
    return JUDGE_TEMPLATE.format(
        removal_codes=_codes(REMOVAL_CODES),
        highlight_codes=_codes(HIGHLIGHT_CODES),
        highlights_criteria=highlights_criteria.strip(),
    )


def rerank_prompt(highlights_criteria: str, shorts_criteria: str) -> str:
    return RERANK_TEMPLATE.format(
        highlights_criteria=highlights_criteria.strip(),
        shorts_criteria=shorts_criteria.strip(),
        highlight_codes=_codes(HIGHLIGHT_CODES),
    )


def chapters_prompt(min_chapters: int, max_chapters: int, min_gap_s: int = 10, max_chapter_min: int = 10) -> str:
    return CHAPTERS_TEMPLATE.format(min_chapters=min_chapters, max_chapters=max_chapters, min_gap_s=min_gap_s,
                                    max_chapter_min=max_chapter_min)


def transcript_lines(items: list[tuple[int, Segment]], prefix: str = "") -> list[str]:
    """'[n] Speaker: text' lines; the speaker only when it changes."""
    lines, last_speaker = [], None
    for n, seg in items:
        speaker = seg.speaker if seg.speaker and seg.speaker != last_speaker else ""
        last_speaker = seg.speaker or last_speaker
        overlap = " (overlap)" if seg.overlap_candidate else ""
        who = f"{speaker}: " if speaker else ""
        lines.append(f"{prefix}[{n}] {who}{seg.text}{overlap}")
    return lines
