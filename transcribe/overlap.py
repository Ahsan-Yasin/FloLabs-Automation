from itertools import pairwise

from core.models import Word


def flag_overlaps(words: list[Word]) -> list[Word]:
    """Cheap, deterministic overlap heuristic (spec section 3.2) — no LLM call.

    For time-adjacent words from different speakers, flag both as
    overlap_candidate when start_b < end_a. Mutates and returns the input list.
    """
    ordered = sorted(words, key=lambda w: w.start)
    for prev, curr in pairwise(ordered):
        if curr.start < prev.end and curr.speaker != prev.speaker:
            prev.overlap_candidate = True
            curr.overlap_candidate = True
    return ordered
