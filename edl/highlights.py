"""Choosing the highlights reel and the shorts from scored segments (plan D6, D7).

Everything here is pure (no LLM, no ffmpeg) so the selection rules can be
tested and tuned in isolation:

1. `candidate_moments`: runs of consecutive segments scoring >= a floor,
   joined across gaps <= highlights_join_gap_s, best `rerank_max_candidates`.
2. (the re-rank call in decide/gemini_client.py calibrates scores, titles and
   windows)
3. `select_highlights`: greedy by score under a time budget, lowering the
   score threshold 7 -> 6 -> 5 -> 4 until the reel is long enough; then
   chronological. Less than highlights_min_s of material -> no reel.
4. `select_shorts`: short-worthy moments, preferred categories first, each
   widened/trimmed at sentence boundaries to shorts_min_s..shorts_max_s.
"""

from __future__ import annotations

from collections import Counter

from core.models import Moment, SegmentJudgment, ShortClip

CANDIDATE_FLOOR = 4
MAX_BRIDGED_SEGMENTS = 3


def candidate_moments(
    judgments: list[SegmentJudgment],
    *,
    floor: int = CANDIDATE_FLOOR,
    join_gap_s: float = 3.0,
    max_candidates: int = 60,
) -> list[Moment]:
    """Group qualifying segments into moments; keep the best `max_candidates`
    (by peak score, then total score), returned in chronological order."""
    groups: list[list[SegmentJudgment]] = []
    for j in judgments:
        if j.highlight_score < floor or j.highlight_category == "none":
            continue
        prev = groups[-1][-1] if groups else None
        # join across a short pause, or a one-to-three line interjection
        # inside the same exchange
        if prev is not None and j.index - prev.index <= MAX_BRIDGED_SEGMENTS + 1 and j.start - prev.end <= join_gap_s:
            groups[-1].append(j)
        else:
            groups.append([j])

    moments = []
    for g in groups:
        peak = max(x.highlight_score for x in g)
        weights = Counter()
        for x in g:
            weights[x.highlight_category] += x.highlight_score
        moments.append((peak, sum(x.highlight_score for x in g), g, weights.most_common(1)[0][0]))
    moments.sort(key=lambda t: (-t[0], -t[1], t[2][0].start))
    chosen = sorted(moments[: max(0, max_candidates)], key=lambda t: t[2][0].start)
    return [
        Moment(
            id=k,
            start=g[0].start,
            end=g[-1].end,
            first_index=g[0].index,
            last_index=g[-1].index,
            score=float(peak),
            category=category,
        )
        for k, (peak, _, g, category) in enumerate(chosen)
    ]


def highlights_budget(duration_s: float, target_s: float, max_fraction: float) -> float:
    return max(0.0, min(target_s, max_fraction * duration_s))


def select_highlights(
    moments: list[Moment],
    *,
    duration_s: float,
    target_s: float = 300.0,
    max_fraction: float = 0.3,
    min_total_s: float = 60.0,
    min_moment_s: float = 8.0,
    thresholds: list[int] | tuple[int, ...] = (7, 6, 5, 4),
    fill_s: float = 180.0,
    judgments: list[SegmentJudgment] | None = None,
) -> list[Moment]:
    """Greedy by score under the budget, lowering the threshold until the reel
    reaches min(fill_s, budget). Returns chosen moments in chronological
    order, or [] when there is not enough qualifying material for a reel.

    With `judgments`, a moment shorter than `min_moment_s` (a one-line
    punchline) is widened with neighbouring sentences instead of discarded."""
    budget = highlights_budget(duration_s, target_s, max_fraction)
    if budget <= 0:
        return []
    if judgments:
        moments = [_widen(m, judgments, min_moment_s) for m in moments]
    want = min(fill_s, budget)
    chosen: dict[int, Moment] = {}
    total = 0.0
    for threshold in thresholds:
        eligible = [m for m in moments if m.score >= threshold and m.duration >= min_moment_s and m.id not in chosen]
        eligible.sort(key=lambda m: (-m.score, -m.duration, m.start))
        for m in eligible:
            if total + m.duration > budget:
                continue
            if any(_overlaps(m, c) for c in chosen.values()):
                continue
            chosen[m.id] = m
            total += m.duration
        if total >= want:
            break
    if total < min_total_s:
        return []
    return sorted(chosen.values(), key=lambda m: m.start)


def _widen(m: Moment, judgments: list[SegmentJudgment], min_s: float) -> Moment:
    if m.duration >= min_s:
        return m
    window = _fit_window(m.first_index, m.last_index, judgments, min_s, max(3 * min_s, 30.0))
    if window is None:
        return m
    first, last = window
    return m.model_copy(update={"first_index": first, "last_index": last,
                                "start": judgments[first].start, "end": judgments[last].end})


def _overlaps(a: Moment, b: Moment) -> bool:
    return a.start < b.end and b.start < a.end


def select_shorts(
    moments: list[Moment],
    judgments: list[SegmentJudgment],
    *,
    count: int = 4,
    min_s: float = 20.0,
    max_s: float = 60.0,
    preferred_categories: list[str] | tuple[str, ...] = ("funny",),
    min_score: float = 5.0,
) -> list[ShortClip]:
    """Short-worthy moments, preferred categories first (by score), then any
    other short-worthy moment by score. Each is widened with neighbouring
    sentences to at least `min_s` and trimmed at a sentence boundary to at
    most `max_s`. Shorts never overlap each other. A short may include
    material the cleanup removed — that is by design."""
    if count <= 0 or not judgments:
        return []
    pool = [m for m in moments if m.short_worthy and m.score >= min_score]
    preferred = sorted((m for m in pool if m.category in preferred_categories), key=lambda m: (-m.score, m.start))
    others = sorted((m for m in pool if m.category not in preferred_categories), key=lambda m: (-m.score, m.start))
    shorts: list[ShortClip] = []
    for m in preferred + others:
        if len(shorts) >= count:
            break
        window = _fit_window(m.first_index, m.last_index, judgments, min_s, max_s)
        if window is None:
            continue
        first, last = window
        start, end = judgments[first].start, judgments[last].end
        if any(start < s.end and s.start < end for s in shorts):
            continue
        shorts.append(ShortClip(index=0, moment_id=m.id, start=start, end=end, score=m.score,
                                category=m.category, title=m.title, hook=m.hook))
    shorts.sort(key=lambda s: -s.score)
    return [s.model_copy(update={"index": k + 1}) for k, s in enumerate(shorts)]


def _fit_window(first: int, last: int, judgments: list[SegmentJudgment], min_s: float, max_s: float):
    """Grow [first, last] one sentence at a time (alternating after/before)
    until it lasts >= min_s; then trim trailing sentences while > max_s.
    Returns None if even one sentence is longer than max_s."""
    n = len(judgments)

    def dur(a, b):
        return judgments[b].end - judgments[a].start

    grow_after = True
    while dur(first, last) < min_s:
        can_after = last + 1 < n and dur(first, last + 1) <= max_s
        can_before = first > 0 and dur(first - 1, last) <= max_s
        if not (can_after or can_before):
            break
        if (grow_after and can_after) or not can_before:
            last += 1
        else:
            first -= 1
        grow_after = not grow_after
    while dur(first, last) > max_s and last > first:
        last -= 1
    if dur(first, last) > max_s:
        return None
    return first, last
