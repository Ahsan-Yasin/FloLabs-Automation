"""Choosing the highlights reel and the shorts from scored segments (plan D6, D7).

Everything here is pure (no LLM, no ffmpeg) so the selection rules can be
tested and tuned in isolation:

1. `candidate_moments`: runs of sentences scoring >= a floor, joined across
   short pauses / up to two bridged lines; the best `max_candidates` go to
   the re-rank. (Funny moments used to enter from a lower floor; since the
   owner asked for highlights/shorts people can LEARN from, they don't.)
2. (the re-rank call in decide/gemini_client.py calibrates scores across the
   whole meeting, writes titles and completes each window)
3. `select_highlights`: best score first above a quality floor, within a time
   budget, with a diversity cap so one stretch of the meeting can't fill the
   reel and a cap on funny moments; over-long moments are trimmed around
   their best sentence; then chronological. Less than `min_total_s` of
   material -> no reel.
4. `select_shorts`: short-worthy moments, preferred (learning) categories
   first, each widened/trimmed at sentence boundaries to
   shorts_min_s..shorts_max_s.

These rules came out of a review of the real 98-minute meeting: the first
version used a 7 -> 6 -> 5 -> 4 threshold ladder, kept only single sentences
as candidates and let 69% of the reel come from 11 minutes of the meeting.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from core.models import Moment, SegmentJudgment, ShortClip


def candidate_moments(
    judgments: list[SegmentJudgment],
    *,
    floor: int = 3,
    funny_floor: int = 3,
    join_gap_s: float = 10.0,
    max_bridge: int = 2,
    max_candidates: int = 60,
) -> list[Moment]:
    """Group qualifying sentences into moments; keep the best `max_candidates`
    (by peak score, then total score), returned in chronological order."""

    def qualifies(j: SegmentJudgment) -> bool:
        # the category is optional (the model often leaves it off a 3-4); the
        # re-rank assigns one anyway
        return j.highlight_score >= (funny_floor if j.highlight_category == "funny" else floor)

    groups: list[list[SegmentJudgment]] = []
    for j in judgments:
        if not qualifies(j):
            continue
        prev = groups[-1][-1] if groups else None
        if prev is not None:
            gap = j.start - prev.end
            # a short pause, or up to max_bridge lines in between (but never
            # across a long silence such as a screen-share setup)
            if gap <= join_gap_s or (j.index - prev.index <= max_bridge + 1 and gap <= 3 * join_gap_s):
                groups[-1].append(j)
                continue
        groups.append([j])

    ranked = []
    for g in groups:
        peak = max(g, key=lambda x: x.highlight_score)
        weights: Counter = Counter()
        for x in g:
            weights[x.highlight_category] += x.highlight_score
        ranked.append((peak.highlight_score, sum(x.highlight_score for x in g), g, peak.index,
                       weights.most_common(1)[0][0]))
    ranked.sort(key=lambda t: (-t[0], -t[1], t[2][0].start))
    chosen = sorted(ranked[: max(0, max_candidates)], key=lambda t: t[2][0].start)
    return [
        Moment(
            id=k,
            start=g[0].start,
            end=g[-1].end,
            first_index=g[0].index,
            last_index=g[-1].index,
            score=float(peak_score),
            category=category,
            peak_index=peak_index,
        )
        for k, (peak_score, _, g, peak_index, category) in enumerate(chosen)
    ]


def calibrate_by_rank(moments: list[Moment], min_raw_score: float = 2.0) -> list[Moment]:
    """Replace each moment's score with one derived from its rank.

    The re-rank's 0-10 scores order the candidates well, but their absolute
    level swings from run to run (the same meeting got a best score of 8 in one
    run and 5 in the next), so a fixed threshold sometimes let nothing through.
    Rank k of N gets 10*(N-k)/N; ties keep the per-sentence peak score as a
    tie-break. The model's own score is kept in `raw_score`, and anything it
    scored below `min_raw_score` gets 0 (never selected), so a meeting with no
    good moments still yields no reel."""
    if not moments:
        return []

    def raw(m: Moment) -> float:
        return m.score if m.reranked else m.score / 2  # un-reranked: a per-sentence peak, less trusted

    order = sorted(moments, key=lambda m: (-raw(m), -m.duration, m.start))
    n = len(order)
    calibrated = {}
    for rank, m in enumerate(order):
        r = raw(m)
        score = 0.0 if (m.reranked and r < min_raw_score) else round(10.0 * (n - rank) / n, 2)
        calibrated[m.id] = m.model_copy(update={"score": score, "raw_score": m.score})
    return [calibrated[m.id] for m in moments]


def highlights_budget(duration_s: float, target_s: float, max_fraction: float) -> float:
    return max(0.0, min(target_s, max_fraction * duration_s))


def select_highlights(
    moments: list[Moment],
    *,
    duration_s: float,
    target_s: float = 300.0,
    max_fraction: float = 0.3,
    min_total_s: float = 60.0,
    min_moment_s: float = 12.0,
    min_score: float = 5.0,
    max_share_per_window: float = 0.35,
    window_s: float = 600.0,
    max_moment_s: float = 60.0,
    max_funny_share: float = 1.0,
    judgments: list[SegmentJudgment] | None = None,
) -> list[Moment]:
    """Best score first (ties: the less-covered part of the meeting, then
    earlier) under the time budget. At most `max_share_per_window` of the
    budget may come from one `window_s` stretch — relaxed in a second pass if
    the reel would otherwise stay under 60% of the budget — and at most
    `max_funny_share` of it from funny moments (never relaxed: the reel is
    for learning, jokes only season it). With `judgments`, short moments are
    widened to `min_moment_s` and long ones are trimmed around their best
    sentence to fit. Returns moments in chronological order, or [] when
    there isn't `min_total_s` of qualifying material."""
    budget = highlights_budget(duration_s, target_s, max_fraction)
    if budget <= 0:
        return []
    pool = [m for m in moments if m.score >= min_score]
    if judgments:
        pool = [_widen(m, judgments, min_moment_s) for m in pool]
    pool = [m for m in pool if m.duration >= min_moment_s or judgments]
    cap = max(max_share_per_window * budget, min_moment_s)
    funny_cap = max_funny_share * budget
    chosen: list[Moment] = []
    used: dict[int, float] = defaultdict(float)
    total = funny = 0.0

    def bucket(m: Moment) -> int:
        return int(m.start // window_s) if window_s > 0 else 0

    for relaxed in (False, True):
        if relaxed and total >= 0.6 * budget:
            break
        taken = {m.id for m in chosen}
        remaining = [m for m in pool if m.id not in taken]
        while remaining:
            remaining.sort(key=lambda m: (-m.score, used[bucket(m)], m.start))
            pick = None
            for m in remaining:
                room = min(budget - total, max_moment_s)
                clip = m if m.duration <= room else _trim_around_peak(m, judgments, room)
                if clip is not None and judgments:
                    clip = _strip_junk_moment(clip, judgments, min_moment_s)
                if clip is None or clip.duration < min_moment_s:
                    continue
                if any(_overlaps(clip, c) for c in chosen):
                    continue
                if not relaxed and used[bucket(clip)] + clip.duration > cap:
                    continue
                if clip.category == "funny" and funny + clip.duration > funny_cap:
                    continue
                pick = (m, clip)
                break
            if pick is None:
                break
            original, clip = pick
            remaining.remove(original)
            chosen.append(clip)
            total += clip.duration
            used[bucket(clip)] += clip.duration
            if clip.category == "funny":
                funny += clip.duration
    if total < min_total_s:
        return []
    return sorted(chosen, key=lambda m: m.start)


def _widen(m: Moment, judgments: list[SegmentJudgment], min_s: float) -> Moment:
    if m.duration >= min_s:
        return m
    window = _fit_window(m.first_index, m.last_index, judgments, min_s, max(3 * min_s, 30.0), m.peak_index)
    if window is None:
        return m
    first, last = window
    return m.model_copy(update={"first_index": first, "last_index": last,
                                "start": judgments[first].start, "end": judgments[last].end})


def _trim_around_peak(m: Moment, judgments: list[SegmentJudgment] | None, max_s: float) -> Moment | None:
    """The longest run of whole sentences inside the moment that contains its
    best sentence and lasts at most `max_s`."""
    if judgments is None or max_s <= 0:
        return None
    peak = m.peak_index if m.peak_index is not None else (m.first_index + m.last_index) // 2
    peak = min(max(peak, m.first_index), m.last_index)
    first = last = peak
    if judgments[peak].end - judgments[peak].start > max_s:
        return None
    grow_after = True
    while True:
        can_after = last < m.last_index and judgments[last + 1].end - judgments[first].start <= max_s
        can_before = first > m.first_index and judgments[last].end - judgments[first - 1].start <= max_s
        if not (can_after or can_before):
            break
        if (grow_after and can_after) or not can_before:
            last += 1
        else:
            first -= 1
        grow_after = not grow_after
    return m.model_copy(update={"first_index": first, "last_index": last,
                                "start": judgments[first].start, "end": judgments[last].end})


def _strip_junk_moment(m: Moment, judgments: list[SegmentJudgment], min_s: float) -> Moment:
    """`_strip_junk` for a reel clip: a moment may bridge removed lines
    (candidate_moments), and the reel is cut straight from the source, so a
    clip starting on a removed "hold on, one second" would put it — and its
    dead air — at the very start of final.mp4."""
    first, last = _strip_junk(m.first_index, m.last_index, judgments, min_s, keep=m.peak_index)
    if (first, last) == (m.first_index, m.last_index):
        return m
    return m.model_copy(update={"first_index": first, "last_index": last,
                                "start": judgments[first].start, "end": judgments[last].end})


def _overlaps(a, b) -> bool:
    return a.start < b.end and b.start < a.end


# ------------------------------------------------------------------ shorts

_JUNK = {"filler", "greeting_small_talk", "housekeeping", "crosstalk", "dead_air"}
# what shorts are mainly for (owner, 2026-09-25): moments people learn from
LEARNING_CATEGORIES = ("concept", "new_architecture", "new_feature", "insight")


def select_shorts(
    moments: list[Moment],
    judgments: list[SegmentJudgment],
    *,
    count: int = 4,
    min_s: float = 20.0,
    max_s: float = 60.0,
    preferred_categories: list[str] | tuple[str, ...] = LEARNING_CATEGORIES,
    min_score: float = 5.0,
) -> list[ShortClip]:
    """Short-worthy moments, preferred categories first (by score) — by
    default the ones people learn from — then any other short-worthy moment
    (e.g. a funny one) by score; short_01 is the best preferred one. When the
    re-rank did not run (no moment is marked reranked), preferred-category
    moments above `min_score` stand in for the missing short-worthy flag.
    Each window is widened with neighbouring sentences to >= `min_s`,
    trimmed to <= `max_s` from the side farther from the moment's core, and
    stripped of filler at either end. Shorts never overlap. A short may
    include material the cleanup removed — by design — but never starts or
    ends on removed filler/small talk/housekeeping."""
    if count <= 0 or not judgments:
        return []
    if any(m.reranked for m in moments):
        pool = [m for m in moments if m.short_worthy and m.score >= min_score]
    else:
        pool = [m for m in moments if m.category in preferred_categories and m.score >= min_score]
    preferred = sorted((m for m in pool if m.category in preferred_categories), key=lambda m: (-m.score, m.start))
    others = sorted((m for m in pool if m.category not in preferred_categories), key=lambda m: (-m.score, m.start))
    shorts: list[ShortClip] = []
    for m in preferred + others:
        if len(shorts) >= count:
            break
        window = _fit_window(m.first_index, m.last_index, judgments, min_s, max_s, m.peak_index)
        if window is None:
            continue
        first, last = _strip_junk(*window, judgments, min_s, keep=m.peak_index)
        start, end = judgments[first].start, judgments[last].end
        if end - start < min_s or end - start > max_s:
            continue
        if any(start < s.end and s.start < end for s in shorts):
            continue
        shorts.append(ShortClip(index=0, moment_id=m.id, start=start, end=end, score=m.score,
                                category=m.category, title=m.title, hook=m.hook))
    # numbered in pick order: preferred (learning) shorts first, best first
    return [s.model_copy(update={"index": k + 1}) for k, s in enumerate(shorts)]


def _strip_junk(first: int, last: int, judgments: list[SegmentJudgment], min_s: float, keep: int | None = None):
    """Drop removed filler/small talk/housekeeping lines from both ends of
    [first, last] while it still lasts >= `min_s` (never the `keep` line)."""
    def junk(i: int) -> bool:
        return i != keep and judgments[i].decision == "remove" and judgments[i].removal_category in _JUNK

    while last > first and junk(last) and judgments[last - 1].end - judgments[first].start >= min_s:
        last -= 1
    while first < last and junk(first) and judgments[last].end - judgments[first + 1].start >= min_s:
        first += 1
    return first, last


def _fit_window(first: int, last: int, judgments: list[SegmentJudgment], min_s: float, max_s: float,
                peak: int | None = None):
    """Grow [first, last] one sentence at a time (alternating after/before)
    until it lasts >= min_s without exceeding max_s. If the core itself is
    longer than max_s, keep the part around `peak` (or the core's middle),
    dropping sentences from the side farther from it. Returns None when not
    even one sentence fits in max_s."""
    n = len(judgments)

    def dur(a, b):
        return judgments[b].end - judgments[a].start

    if dur(first, last) > max_s:
        centre = peak if peak is not None and first <= peak <= last else (first + last) // 2
        while dur(first, last) > max_s and last > first:
            if centre - first > last - centre:
                first += 1
            else:
                last -= 1
        if dur(first, last) > max_s:
            return None
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
    return first, last
