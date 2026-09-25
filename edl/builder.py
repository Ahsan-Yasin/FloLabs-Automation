import bisect
import itertools
from fractions import Fraction

from core.config import get_settings
from core.logging import get_logger
from core.models import Decision, EditDecisionList, EDLRange, RemovedRange, Word
from core.timeline import parse_rate, rate_str, round_half_up

logger = get_logger(__name__)


def _keep_ranges(decisions: list[Decision]) -> list[tuple[float, float]]:
    ranges = [(d.start, d.end) for d in decisions if d.decision == "keep" and d.end > d.start]
    ranges.sort(key=lambda r: r[0])
    return ranges


def _coalesce_overlaps(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Never trust LLM ordering (section 3.4) — sort and merge any overlapping/touching ranges."""
    if not ranges:
        return []
    ranges = sorted(ranges, key=lambda r: r[0])
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


def _merge_small_gaps(ranges: list[tuple], min_gap) -> list[tuple]:
    """Merge KEEP ranges separated by < min_gap (avoids choppy micro-cuts)."""
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


def _drop_or_merge_tiny_segments(ranges: list[tuple], min_duration) -> list[tuple]:
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


def build_edl(
    decisions: list[Decision],
    words: list[Word],
    source_duration: float,
    fps: "str | Fraction | None" = None,
    fade_frames: int = 0,
    min_segment_s: float | None = None,
    full_video_fallback: bool = True,
    total_frames: int | None = None,
    min_gap_s: float | None = None,
) -> EditDecisionList:
    """Turn keep/remove decisions into a validated EDL.

    Without `fps` this is the original seconds-based builder. With `fps` (v2)
    every boundary is additionally snapped to the source frame grid and the
    result is safe to render with `fade_frames`-long dissolves:

    coalesce -> snap to words -> snap to frames -> coalesce -> merge gaps
    shorter than max(min_gap_merge_seconds, (d+1)/F) (put back, never shown as
    a cut) -> merge/drop keeps shorter than max(min_segment_s, 2d/F) (2d/F
    only when there are 2+ keeps) -> clamp to the source -> validate.
    """
    settings = get_settings()
    min_segment_s = settings.min_segment_seconds if min_segment_s is None else min_segment_s
    min_gap_s = settings.min_gap_merge_seconds if min_gap_s is None else min_gap_s

    ranges = _keep_ranges(decisions)
    ranges = _coalesce_overlaps(ranges)
    ranges = _snap_to_word_boundaries(ranges, words)
    ranges = _coalesce_overlaps(ranges)  # snapping can push neighbors into overlap

    if fps is None:
        return _finish_seconds_edl(ranges, source_duration, settings, min_segment_s, full_video_fallback, min_gap_s)
    return _finish_frame_edl(
        ranges, source_duration, parse_rate(fps), fade_frames, settings, min_segment_s, full_video_fallback,
        total_frames, min_gap_s,
    )


def _finish_seconds_edl(
    ranges, source_duration, settings, min_segment_s, full_video_fallback, min_gap_s
) -> EditDecisionList:
    before = len(ranges)
    gap_total = _gap_total(ranges)
    ranges = _merge_small_gaps(ranges, min_gap_s)
    merged_count, merged_seconds = before - len(ranges), gap_total - _gap_total(ranges)
    ranges = _drop_or_merge_tiny_segments(ranges, min_segment_s)

    fallback = False
    if not ranges:
        if not full_video_fallback:
            return EditDecisionList(ranges=[], source_duration=source_duration)
        # No real crosstalk / LLM removed everything: safe no-op over the whole file
        # rather than emitting an empty video (section 7).
        logger.warning("EDL ended up empty after filtering — falling back to full-video KEEP range")
        ranges = [(0.0, source_duration)]
        fallback = True

    ranges = [(max(0.0, s), min(e, source_duration) if source_duration else e) for s, e in ranges]
    ranges = [(s, e) for s, e in ranges if e > s]
    edl = EditDecisionList(
        ranges=[EDLRange(start=s, end=e) for s, e in ranges],
        source_duration=source_duration,
        merged_gap_count=merged_count,
        merged_gap_seconds=round(float(merged_seconds), 3),
        used_full_video_fallback=fallback,
    )
    validate_edl(edl)
    return edl


def _gap_total(ranges) -> float:
    return sum(b[0] - a[1] for a, b in itertools.pairwise(ranges))


def _fix_short_keeps(
    fr: list[tuple[int, int]], min_keep: int, min_gap: int, absorb_gap: int, total: int
) -> tuple[list[tuple[int, int]], list[int]]:
    """Make every keep at least `min_keep` frames without resurrecting removed
    content (frame-grid path only).

    The legacy rule merged a short keep into its next neighbour, which pulls
    the whole removed gap between them back into the video — a one-second
    "Sounds good." between two cut tangents brought 7 s of cut small talk
    back. Instead, in order of preference:
      1. widen the keep into the gaps on either side (each gap stays >= min_gap);
      2. otherwise merge it with the neighbour across the smaller gap, but
         only if that gap is <= absorb_gap (too short to show as a cut anyway);
      3. otherwise drop it.
    Returns the new ranges and the sizes (frames) of the gaps absorbed in 2.
    """
    fr = list(fr)
    absorbed: list[int] = []
    i = 0
    while i < len(fr):
        s, e = fr[i]
        need = min_keep - (e - s)
        if need <= 0:
            i += 1
            continue
        left_room = max(0, s - fr[i - 1][1] - min_gap) if i > 0 else s
        right_room = max(0, fr[i + 1][0] - e - min_gap) if i < len(fr) - 1 else total - e
        if left_room + right_room >= need:
            take_left = min(left_room, (need + 1) // 2)
            take_right = min(right_room, need - take_left)
            take_left = need - take_right
            fr[i] = (s - take_left, e + take_right)
            i += 1
            continue
        gap_left = s - fr[i - 1][1] if i > 0 else None
        gap_right = fr[i + 1][0] - e if i < len(fr) - 1 else None
        options = [(g, side) for g, side in ((gap_left, "l"), (gap_right, "r")) if g is not None and g <= absorb_gap]
        if options:
            gap, side = min(options)
            absorbed.append(gap)
            if side == "l":
                fr[i - 1] = (fr[i - 1][0], e)
                fr.pop(i)
                i -= 1  # re-check the merged range
            else:
                fr[i] = (s, fr[i + 1][1])
                fr.pop(i + 1)
            continue
        fr.pop(i)
    return fr, absorbed


def _finish_frame_edl(
    ranges, source_duration, fps: Fraction, d: int, settings, min_segment_s, full_video_fallback, total_frames,
    min_gap_s,
) -> EditDecisionList:
    if d % 2:
        raise ValueError(f"fade_frames must be even, got {d}")
    total = total_frames if total_frames is not None else round_half_up(Fraction(source_duration) * fps)
    h = d // 2

    def to_f(t: float) -> int:
        return round_half_up(Fraction(t) * fps)

    fr = [(max(0, to_f(s)), min(total, to_f(e))) for s, e in ranges]
    fr = [(s, e) for s, e in fr if e > s and s < total]
    fr = _coalesce_overlaps(fr)

    min_gap = max(to_f(min_gap_s), d + 1)
    before = len(fr)
    gap_total = _gap_total(fr)
    fr = _merge_small_gaps(fr, min_gap)
    merged_count, merged_frames = before - len(fr), gap_total - _gap_total(fr)

    # 2d only matters where there are cuts to dissolve across; a lone keep
    # shorter than that (in a source shorter than 2d frames) would otherwise
    # be dropped and wrongly reported as the "nothing kept" fallback.
    min_keep = max(to_f(min_segment_s), 2 * d if len(fr) > 1 else 0, 1)
    fr, absorbed = _fix_short_keeps(fr, min_keep, min_gap, to_f(settings.removed_video_min_gap_s), total)
    merged_count += len(absorbed)
    merged_frames += sum(absorbed)

    fallback = False
    if not fr:
        if not full_video_fallback:
            return EditDecisionList(ranges=[], source_duration=source_duration, fps=rate_str(fps),
                                    fade_frames=d, total_frames=total)
        logger.warning("EDL ended up empty after filtering — falling back to full-video KEEP range")
        fr = [(0, total)]
        fallback = True

    # A removed sliver at the very start/end shorter than h can't be seen
    # anyway — keep the file edge instead of cutting a few frames.
    if fr[0][0] < h:
        fr[0] = (0, fr[0][1])
    if total - fr[-1][1] < h:
        fr[-1] = (fr[-1][0], total)

    edl = EditDecisionList(
        ranges=[
            EDLRange(start=float(Fraction(s) / fps), end=float(Fraction(e) / fps), start_frame=s, end_frame=e)
            for s, e in fr
        ],
        source_duration=source_duration,
        fps=rate_str(fps),
        fade_frames=d,
        total_frames=total,
        merged_gap_count=merged_count,
        merged_gap_seconds=round(float(Fraction(merged_frames) / fps), 3),
        used_full_video_fallback=fallback,
    )
    validate_edl(edl)
    return edl


def validate_edl(edl: EditDecisionList) -> None:
    """Assert the EDL invariants from section 9: sorted, non-overlapping; on a
    frame grid also: whole frames inside the source, gaps and keeps long
    enough for the dissolve. A lone range has no cut and needs no dissolve,
    so it may be shorter than 2d (a source shorter than 2d frames must still
    produce a valid EDL, e.g. its full-video fallback)."""
    prev_end = None
    prev_end_frame = None
    d = edl.fade_frames
    for r in edl.ranges:
        if r.end <= r.start:
            raise ValueError(f"EDL range has non-positive duration: {r}")
        if prev_end is not None and r.start < prev_end:
            raise ValueError(f"EDL ranges overlap or are out of order: prev_end={prev_end}, next={r}")
        if edl.fps is not None:
            if r.start_frame is None or r.end_frame is None:
                raise ValueError(f"frame-grid EDL range is missing frame indices: {r}")
            if r.start_frame < 0 or (edl.total_frames is not None and r.end_frame > edl.total_frames):
                raise ValueError(f"EDL range outside the source: {r}")
            if d and len(edl.ranges) > 1 and r.end_frame - r.start_frame < 2 * d:
                raise ValueError(f"EDL range shorter than two dissolves ({2 * d} frames): {r}")
            if prev_end_frame is not None and d and r.start_frame - prev_end_frame < d + 1:
                raise ValueError(f"EDL gap shorter than the dissolve before {r}")
            prev_end_frame = r.end_frame
        prev_end = r.end


def complement(edl: EditDecisionList, video_min_gap_s: float | None = None) -> list[RemovedRange]:
    """The removed parts: exactly the stretches of [0, duration] not kept.

    Removed ranges and kept ranges tile the source with no gap or overlap.
    Tier "video" (>= removed_video_min_gap_s) also goes into removed.mp4;
    shorter ones are "transcript_only".
    """
    if video_min_gap_s is None:
        video_min_gap_s = get_settings().removed_video_min_gap_s
    removed: list[RemovedRange] = []

    def add(s, e, sf=None, ef=None):
        if e - s <= 0:
            return
        tier = "video" if e - s >= video_min_gap_s - 1e-9 else "transcript_only"
        removed.append(RemovedRange(start=s, end=e, start_frame=sf, end_frame=ef, tier=tier))

    if edl.fps is not None:
        fps = parse_rate(edl.fps)
        total = edl.total_frames if edl.total_frames is not None else round_half_up(
            Fraction(edl.source_duration) * fps)
        cursor = 0
        for r in edl.ranges:
            if r.start_frame > cursor:
                add(float(Fraction(cursor) / fps), float(Fraction(r.start_frame) / fps), cursor, r.start_frame)
            cursor = r.end_frame
        if total > cursor:
            add(float(Fraction(cursor) / fps), float(Fraction(total) / fps), cursor, total)
        return removed

    cursor = 0.0
    for r in edl.ranges:
        if r.start > cursor:
            add(cursor, r.start)
        cursor = r.end
    if edl.source_duration > cursor:
        add(cursor, edl.source_duration)
    return removed
