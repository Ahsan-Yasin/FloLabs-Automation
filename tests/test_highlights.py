from core.models import Moment, SegmentJudgment
from edl.highlights import (
    calibrate_by_rank,
    candidate_moments,
    highlights_budget,
    select_highlights,
    select_shorts,
)


def _j(i, score=0, cat="none", start=None, length=4.0, gap=1.0, decision="keep", removal="none", text=None):
    start = i * (length + gap) if start is None else start
    return SegmentJudgment(index=i, start=start, end=start + length, text=text or f"s{i}.", decision=decision,
                           removal_category=removal, highlight_score=score, highlight_category=cat)


def _m(id_, start, end, score, cat="concept", short=False, first=0, last=0, peak=None, reranked=True):
    return Moment(id=id_, start=start, end=end, first_index=first, last_index=last, score=score, category=cat,
                  short_worthy=short, peak_index=peak, reranked=reranked)


# ---------------------------------------------------------------- candidates


def test_candidates_join_across_short_pauses_and_bridge_two_lines():
    js = [_j(0), _j(1, 8, "funny"), _j(2, 7, "funny"), _j(3, 1), _j(4, 1), _j(5, 6, "funny"), _j(6), _j(7), _j(8),
          _j(9, 9, "decision")]
    moments = candidate_moments(js)
    assert [(m.first_index, m.last_index) for m in moments] == [(1, 5), (9, 9)]
    assert moments[0].score == 8 and moments[0].category == "funny" and moments[0].peak_index == 1
    assert moments[1].category == "decision"


def test_candidates_do_not_join_long_pauses_or_many_lines():
    js = [_j(0, 8, "funny", start=0.0), _j(1, 8, "funny", start=50.0)]  # 46 s silence
    assert len(candidate_moments(js)) == 2
    js = [_j(0, 8, "funny")] + [_j(i, 0) for i in range(1, 4)] + [_j(4, 8, "funny")]
    assert len(candidate_moments(js)) == 2  # 3 lines (16 s) in between: separate


def test_floor_is_score_three_for_everything_including_jokes():
    js = [_j(0, 3, "concept"), _j(5, 2, "funny", start=100.0), _j(10, 2, "concept", start=200.0),
          _j(15, 9, "none", start=300.0), _j(20, 2, "none", start=400.0)]
    # a high score counts even without a category (the re-rank assigns one);
    # a mild joke (2) no longer gets a head start
    assert [m.first_index for m in candidate_moments(js)] == [0, 15]


def test_funny_moments_fill_at_most_a_third_of_the_reel():
    jokes = [_m(i, 700 * i, 700 * i + 40, 9, "funny") for i in range(5)]  # the best-scored moments
    learning = [_m(10 + k, 350 + 700 * k, 390 + 700 * k, 6, "concept") for k in range(5)]
    chosen = select_highlights(jokes + learning, duration_s=3600, target_s=300, max_funny_share=0.34)
    funny_s = sum(m.duration for m in chosen if m.category == "funny")
    assert 0 < funny_s <= 0.34 * 300
    assert sum(m.duration for m in chosen if m.category == "concept") >= 160


def test_candidates_capped_to_the_best_and_returned_in_time_order():
    js = [_j(i, 4 + (i % 7), "concept", start=i * 100.0) for i in range(30)]
    moments = candidate_moments(js, max_candidates=5)
    assert len(moments) == 5
    assert sorted(m.score for m in moments) == [9, 10, 10, 10, 10]
    assert [m.start for m in moments] == sorted(m.start for m in moments)
    assert [m.id for m in moments] == list(range(5))


# ---------------------------------------------------------------- reel


def test_budget_is_capped_for_short_meetings():
    assert highlights_budget(3600, 300, 0.3) == 300
    assert highlights_budget(600, 300, 0.3) == 180


def test_best_score_first_within_budget_then_chronological():
    moments = [_m(0, 0, 50, 9), _m(1, 700, 760, 8), _m(2, 1400, 1450, 7), _m(3, 2100, 2150, 6), _m(4, 2800, 2850, 4)]
    chosen = select_highlights(moments, duration_s=3600, target_s=300, min_score=5)
    assert [m.id for m in chosen] == [0, 1, 2, 3]  # 4 is below the quality floor
    assert [m.start for m in chosen] == sorted(m.start for m in chosen)


def test_diversity_cap_spreads_the_reel_over_the_meeting():
    # six strong moments inside one 10-minute stretch, weaker ones elsewhere
    crowded = [_m(i, 60 * i, 60 * i + 40, 9) for i in range(6)]
    spread = [_m(10 + k, 1000 + 700 * k, 1040 + 700 * k, 6) for k in range(4)]
    chosen = select_highlights(crowded + spread, duration_s=3600, target_s=300, max_share_per_window=0.35)
    in_crowd = sum(m.duration for m in chosen if m.start < 600)
    assert in_crowd <= 0.35 * 300
    assert any(m.id >= 10 for m in chosen)


def test_diversity_cap_is_relaxed_when_the_reel_would_stay_short():
    crowded = [_m(i, 60 * i, 60 * i + 40, 9) for i in range(6)]
    chosen = select_highlights(crowded, duration_s=3600, target_s=300, max_share_per_window=0.35)
    assert sum(m.duration for m in chosen) >= 0.6 * 300


def test_no_reel_when_too_little_material():
    assert select_highlights([_m(0, 0, 40, 9)], duration_s=3600, min_total_s=60) == []


def test_short_moments_are_widened_with_neighbouring_sentences():
    js = [_j(i) for i in range(40)]  # 4 s sentences, 1 s gaps
    punchline = _m(0, js[10].start, js[10].end, 9, "funny", first=10, last=10, peak=10)
    other = _m(1, js[20].start, js[35].end, 8, first=20, last=35, peak=25)
    chosen = select_highlights([punchline, other], duration_s=3600, min_total_s=10, judgments=js,
                               min_moment_s=12.0)
    widened = next(m for m in chosen if m.id == 0)
    assert widened.duration >= 12.0 and widened.first_index <= 10 <= widened.last_index


def test_long_moments_are_trimmed_around_their_best_sentence():
    js = [_j(i) for i in range(60)]
    long_one = _m(0, js[0].start, js[59].end, 9, first=0, last=59, peak=40)
    (clip,) = select_highlights([long_one], duration_s=3600, min_total_s=10, judgments=js, max_moment_s=60)
    assert clip.duration <= 60 and clip.first_index <= 40 <= clip.last_index


def test_overlapping_moments_are_not_both_chosen():
    moments = [_m(0, 0, 60, 9), _m(1, 30, 90, 8), _m(2, 1000, 1060, 7)]
    assert [m.id for m in select_highlights(moments, duration_s=3600, min_total_s=10)] == [0, 2]


# ---------------------------------------------------------------- shorts


def test_shorts_prefer_learning_moments_then_fill_and_never_overlap():
    """Owner: shorts are mainly for learning; a funny one only fills a slot."""
    js = [_j(i, length=9.0) for i in range(60)]  # 9 s sentences, 1 s gaps
    moments = [
        _m(0, js[2].start, js[3].end, 7, "concept", True, 2, 3),
        _m(1, js[10].start, js[11].end, 10, "funny", True, 10, 11),
        _m(2, js[20].start, js[21].end, 6, "new_architecture", True, 20, 21),
        _m(3, js[3].start, js[4].end, 8, "insight", True, 3, 4),  # overlaps short 0... picked first (8 > 7)
        _m(4, js[40].start, js[41].end, 9, "concept", False, 40, 41),  # not short-worthy
    ]
    shorts = select_shorts(moments, js, count=3, min_s=20, max_s=60)
    assert [s.moment_id for s in shorts] == [3, 2, 1]  # learning ones first (best first), then funny
    assert [s.index for s in shorts] == [1, 2, 3]
    assert all(20 <= s.end - s.start <= 60 for s in shorts)
    assert [s.moment_id for s in select_shorts(moments, js, count=2, min_s=20, max_s=60)] == [3, 2]


def test_short_windows_keep_the_core_and_snap_to_sentences():
    js = [_j(i, length=9.0) for i in range(30)]
    tiny = _m(0, js[10].start, js[10].end, 9, "funny", True, 10, 10, peak=10)
    huge = _m(1, js[15].start, js[29].end, 9, "funny", True, 15, 29, peak=27)
    shorts = select_shorts([tiny, huge], js, count=2, min_s=20, max_s=60)
    starts, ends = {j.start for j in js}, {j.end for j in js}
    for s in shorts:
        assert s.start in starts and s.end in ends and 20 <= s.end - s.start <= 60
    big = next(s for s in shorts if s.moment_id == 1)
    assert big.start <= js[27].start and big.end >= js[27].end  # trimmed from the side away from the peak


def test_trailing_filler_is_dropped_from_shorts():
    js = [_j(i, length=9.0) for i in range(10)]
    js[5] = _j(5, length=9.0, decision="remove", removal="filler", text="okay okay")
    short = _m(0, js[2].start, js[5].end, 9, "funny", True, 2, 5)
    (s,) = select_shorts([short], js, count=1, min_s=20, max_s=60)
    assert s.end == js[4].end


def test_shorts_fall_back_to_categories_when_the_rerank_failed():
    js = [_j(i, length=9.0) for i in range(10)]
    moment = _m(0, js[2].start, js[3].end, 7, "concept", short=False, first=2, last=3, reranked=False)
    assert len(select_shorts([moment], js, count=2, min_s=20, max_s=60)) == 1
    joke = moment.model_copy(update={"category": "funny"})
    assert select_shorts([joke], js, count=2, min_s=20, max_s=60) == []  # not a learning moment


def test_no_shorts_requested_or_available():
    js = [_j(i) for i in range(5)]
    assert select_shorts([_m(0, 0, 10, 9, "funny", True, 0, 1)], js, count=0) == []
    assert select_shorts([_m(0, 0, 10, 9, "funny", False, 0, 1)], js, count=4) == []
    assert select_shorts([_m(0, 0, 10, 3, "funny", True, 0, 1)], js, count=4) == []  # below min score


# ---------------------------------------------------------------- calibration


def test_rank_calibration_ignores_the_absolute_level_of_rerank_scores():
    """The same meeting got a best re-rank score of 8 in one run and 5 in the
    next; ranks make the reel independent of that swing."""
    low = [_m(i, i * 100, i * 100 + 30, s) for i, s in enumerate([5, 3, 4, 2, 3])]
    high = [_m(i, i * 100, i * 100 + 30, s) for i, s in enumerate([9, 7, 8, 6, 7])]
    assert [m.score for m in calibrate_by_rank(low)] == [m.score for m in calibrate_by_rank(high)]
    cal = calibrate_by_rank(low)
    assert cal[0].score == 10.0 and cal[0].raw_score == 5
    assert sorted(m.score for m in cal) == sorted(m.score for m in cal)  # order preserved in the list


def test_weak_moments_never_qualify_even_at_the_top_of_a_weak_meeting():
    weak = [_m(0, 0, 30, 1), _m(1, 100, 130, 1)]
    assert all(m.score == 0 for m in calibrate_by_rank(weak, min_raw_score=2))
    assert select_highlights(calibrate_by_rank(weak), duration_s=3600, min_total_s=10) == []


def test_unreranked_moments_rank_below_reranked_ones_with_the_same_number():
    moments = [_m(0, 0, 30, 6, reranked=False), _m(1, 100, 130, 6)]
    cal = {m.id: m.score for m in calibrate_by_rank(moments)}
    assert cal[1] > cal[0]


def test_removed_filler_at_either_end_of_a_short_is_dropped():
    js = [_j(i, length=9.0) for i in range(10)]
    js[2] = _j(2, length=9.0, decision="remove", removal="housekeeping", text="sorry, one second")
    short = _m(0, js[2].start, js[5].end, 9, "concept", True, 2, 5, peak=4)
    (s,) = select_shorts([short], js, count=1, min_s=20, max_s=60)
    assert (s.start, s.end) == (js[3].start, js[5].end)


def test_a_reel_clip_never_starts_or_ends_on_removed_housekeeping():
    """Live case: a moment bridged a removed "sorry, I have an emergency
    outside" line full of dead air, and final.mp4 opened on it."""
    js = [_j(i, length=9.0) for i in range(30)]
    js[10] = _j(10, length=9.0, decision="remove", removal="housekeeping", text="sorry, one second")
    js[16] = _j(16, length=9.0, decision="remove", removal="filler", text="okay okay")
    moment = _m(0, js[10].start, js[16].end, 9, first=10, last=16, peak=13)
    (clip,) = select_highlights([moment], duration_s=3600, min_total_s=10, judgments=js)
    assert (clip.first_index, clip.last_index) == (11, 15)
    assert (clip.start, clip.end) == (js[11].start, js[15].end)
