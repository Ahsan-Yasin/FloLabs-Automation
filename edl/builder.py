import bisect

from core.config import get_settings
from core.logging import get_logger
from core.models import Decision, EditDecisionList, EDLRange, Word

logger = get_logger(__name__)


def _keep_ranges(decisions: list[Decision]) -> list[tuple[float, float]]:
    ranges = [(d.start, d.end) for d in decisions if d.decision == "keep" and d.end > d.start]
    ranges.sort(key=lambda r: r[0])
    return ranges


def _coalesce_overlaps(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Never trust LLM ordering (section 3.4) — sort and merge any overlapping/touching ranges."""
    if not ranges:
        return []
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _snap_to_word_boundaries(
    ranges: list[tuple[float, float]], words: list[Word]
) -> list[tuple[float, float]]:
    """Never cut mid-word; expand outward rather than clip a word (section 3.4)."""
    if not words:
        return ranges

    starts = [w.start for w in words]

    def word_at_index(t: float) -> Word | None:
        idx = bisect.bisect_right(starts, t) - 1
        return words[idx] if idx >= 0 else None

    def snap_start(t: float) -> float:
        # If t lands inside a word (not exactly on its start), expand left to
        # include the whole word rather than clipping it.
        w = word_at_index(t)
        if w and w.start < t < w.end:
            return w.start
        return t

    def snap_end(t: float) -> float:
        # If t lands inside a word (not exactly on its end), expand right to
        # include the whole word rather than clipping it.
        w = word_at_index(t)
        if w and w.start < t < w.end:
            return w.end
        return t

    snapped = []
    for start, end in ranges:
        new_start = snap_start(start)
        new_end = snap_end(end)
        snapped.append((new_start, max(new_end, new_start)))
    return snapped


def _merge_small_gaps(ranges: list[tuple[float, float]], min_gap: float) -> list[tuple[float, float]]:
    """Merge KEEP ranges separated by < min_gap seconds (avoids choppy micro-cuts)."""
    if not ranges:
        return []
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        last_start, last_end = merged[-1]
        if start - last_end < min_gap:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _drop_or_merge_tiny_segments(
    ranges: list[tuple[float, float]], min_duration: float
) -> list[tuple[float, float]]:
    """Merge KEEP ranges under ~min_duration into a neighbor, or drop if isolated."""
    result = list(ranges)
    changed = True
    while changed:
        changed = False
        for i, (start, end) in enumerate(result):
            if end - start >= min_duration:
                continue
            if len(result) == 1:
                result.pop(i)
            elif i == len(result) - 1:
                prev_start, prev_end = result[i - 1]
                result[i - 1] = (prev_start, max(prev_end, end))
                result.pop(i)
            else:
                _, next_end = result[i + 1]
                result[i] = (start, next_end)
                result.pop(i + 1)
            changed = True
            break
    return result


def build_edl(decisions: list[Decision], words: list[Word], source_duration: float) -> EditDecisionList:
    settings = get_settings()

    ranges = _keep_ranges(decisions)
    ranges = _coalesce_overlaps(ranges)
    ranges = _snap_to_word_boundaries(ranges, words)
    ranges = _coalesce_overlaps(ranges)  # snapping can push neighbors into overlap
    ranges = _merge_small_gaps(ranges, settings.min_gap_merge_seconds)
    ranges = _drop_or_merge_tiny_segments(ranges, settings.min_segment_seconds)

    if not ranges:
        # No real crosstalk / LLM removed everything: safe no-op over the whole file
        # rather than emitting an empty video (section 7).
        logger.warning("EDL ended up empty after filtering — falling back to full-video KEEP range")
        ranges = [(0.0, source_duration)]

    ranges = [(max(0.0, s), min(e, source_duration) if source_duration else e) for s, e in ranges]
    edl = EditDecisionList(
        ranges=[EDLRange(start=s, end=e) for s, e in ranges],
        source_duration=source_duration,
    )
    validate_edl(edl)
    return edl


def validate_edl(edl: EditDecisionList) -> None:
    """Assert the EDL invariants from section 9: sorted, non-overlapping."""
    prev_end = None
    for r in edl.ranges:
        if r.end <= r.start:
            raise ValueError(f"EDL range has non-positive duration: {r}")
        if prev_end is not None and r.start < prev_end:
            raise ValueError(f"EDL ranges overlap or are out of order: prev_end={prev_end}, next={r}")
        prev_end = r.end
