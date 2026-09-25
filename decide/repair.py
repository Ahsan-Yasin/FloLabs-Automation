"""Deterministic clean-up of the AI's keep/remove decisions.

Caption-derived "sentences" are sometimes cut by the 40-word / 20-second cap
or a cue boundary, so the tail of a kept sentence ("…done with for the" /
"legal.") can arrive as its own short segment. If the model removes that tail,
the cleaned video cuts a sentence in half — the most audible defect in the
review of the real 98-minute meeting (7 cases). This pass puts such fragments
back, without any extra AI call.
"""

from __future__ import annotations

import re

from core.models import SegmentJudgment

_SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")
MAX_FRAGMENT_S = 3.5
MAX_FRAGMENT_WORDS = 5
MAX_JOIN_GAP_S = 1.0


def _is_fragment(j: SegmentJudgment) -> bool:
    return (j.end - j.start) <= MAX_FRAGMENT_S or len(j.text.split()) <= MAX_FRAGMENT_WORDS


def _ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END.search(text.strip()))


def repair_fragments(judgments: list[SegmentJudgment]) -> tuple[list[SegmentJudgment], int]:
    """Keep a short removed segment that (a) finishes a kept sentence by the
    same speaker, or (b) starts a kept sentence by the same speaker that
    continues in lowercase. Returns (judgments, number repaired)."""
    out = list(judgments)
    repaired = 0
    for i, j in enumerate(out):
        if j.decision != "remove" or not _is_fragment(j):
            continue
        prev = out[i - 1] if i > 0 else None
        nxt = out[i + 1] if i + 1 < len(out) else None
        tail_of_prev = (
            prev is not None and prev.decision == "keep" and prev.speaker == j.speaker
            and not _ends_sentence(prev.text) and j.start - prev.end <= MAX_JOIN_GAP_S
        )
        head_of_next = (
            nxt is not None and nxt.decision == "keep" and nxt.speaker == j.speaker
            and not _ends_sentence(j.text) and nxt.text[:1].islower() and nxt.start - j.end <= MAX_JOIN_GAP_S
        )
        if tail_of_prev or head_of_next:
            out[i] = j.model_copy(update={"decision": "keep", "removal_category": "none",
                                          "reason": "completes a kept sentence"})
            repaired += 1
    return out, repaired
