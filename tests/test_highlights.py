from core.models import Moment, SegmentJudgment
from edl.highlights import (
    candidate_moments,
    highlights_budget,
    select_highlights,
    select_shorts,
)


def _j(i, score=0, cat="none", start=None, length=4.0, gap=1.0):
    start = i * (length + gap) if start is None else start
    return SegmentJudgment(index=i, start=start, end=start + length, text=f"s{i}", decision="keep",
                           highlight_score=score, highlight_category=cat)


def _m(id_, start, end, score, cat="concept", short=False, first=0, last=0):
    return Moment(id=id_, start=start, end=end, first_index=first, last_index=last, score=score, category=cat,
                  short_worthy=short)


# ---------------------------------------------------------------- candidates


def test_candidates_group_consecutive_and_bridge_short_interjections():
    # a 1 s interjection ("ha!") between two funny lines keeps them together
    js = [_j(0), _j(1, 8, "funny"), _j(2, 7, "funny"), _j(3, 1, start=14.5, length=1.0),
          _j(4, 6, "funny", start=17.0), _j(5, start=22.0), _j(6, start=27.0), _j(7, start=32.0),
          _j(8, start=37.0), _j(9, 9, "decision", start=42.0)]
    moments = candidate_moments(js, join_gap_s=3.0)
    assert [(m.first_index, m.last_index) for m in moments] == [(1, 4), (9, 9)]
    assert moments[0].score == 8 and moments[0].category == "funny"
    assert moments[1].category == "decision"


def test_candidates_do_not_bridge_long_pauses_or_many_lines():
    js = [_j(0, 8, "funny", start=0.0), _j(1, 8, "funny", start=20.0)]  # 16 s pause
    assert len(candidate_moments(js, join_gap_s=3.0)) == 2
    js = [_j(0, 8, "funny")] + [_j(i, 0) for i in range(1, 6)] + [_j(6, 8, "funny")]
    assert len(candidate_moments(js, join_gap_s=100.0)) == 2  # 5 lines in between: separate


def test_candidates_capped_to_the_best_and_returned_in_time_order():
    js = [_j(i, 4 + (i % 7), "concept", start=i * 100.0) for i in range(30)]
    moments = candidate_moments(js, max_candidates=5)
    assert len(moments) == 5
    assert sorted(m.score for m in moments) == [9, 10, 10, 10, 10]
    assert [m.start for m in moments] == sorted(m.start for m in moments)
    assert [m.id for m in moments] == list(range(5))


def test_candidates_ignore_uncategorised_scores():
    assert candidate_moments([_j(0, 9, "none")]) == []


# ---------------------------------------------------------------- reel


def test_budget_is_capped_for_short_meetings():
    assert highlights_budget(3600, 300, 0.3) == 300
    assert highlights_budget(600, 300, 0.3) == 180


def test_greedy_by_score_within_budget_then_chronological():
    moments = [_m(0, 0, 100, 9), _m(1, 200, 330, 8), _m(2, 400, 480, 7), _m(3, 500, 520, 6)]
    chosen = select_highlights(moments, duration_s=3600, target_s=300, fill_s=180)
    # 100 + 130 = 230 >= fill 180 at threshold 7; 80 more would exceed... no: 310 > 300
    assert [m.id for m in chosen] == [0, 1]
    assert [m.start for m in chosen] == sorted(m.start for m in chosen)


def test_threshold_backs_off_until_the_reel_is_long_enough():
    moments = [_m(0, 0, 50, 7), _m(1, 100, 170, 5), _m(2, 200, 280, 4)]
    chosen = select_highlights(moments, duration_s=3600, fill_s=180)
    assert [m.id for m in chosen] == [0, 1, 2]


def test_no_reel_when_too_little_material():
    assert select_highlights([_m(0, 0, 40, 9)], duration_s=3600, min_total_s=60) == []


def test_short_moments_are_widened_with_neighbouring_sentences():
    js = [_j(i) for i in range(20)]  # 4 s sentences, 1 s gaps
    punchline = _m(0, js[10].start, js[10].end, 9, "funny", first=10, last=10)
    long_one = _m(1, js[0].start, js[8].end, 8, first=0, last=8)
    chosen = select_highlights([punchline, long_one], duration_s=3600, min_total_s=10, judgments=js,
                               min_moment_s=8.0)
    widened = next(m for m in chosen if m.id == 0)
    assert widened.duration >= 8.0 and widened.first_index <= 10 <= widened.last_index
    # without judgments the 4 s moment is simply not eligible
    assert [m.id for m in select_highlights([punchline, long_one], duration_s=3600, min_total_s=10)] == [1]


def test_overlapping_moments_are_not_both_chosen():
    moments = [_m(0, 0, 60, 9), _m(1, 30, 90, 8), _m(2, 100, 160, 7)]
    assert [m.id for m in select_highlights(moments, duration_s=3600, min_total_s=10)] == [0, 2]


# ---------------------------------------------------------------- shorts


def test_shorts_prefer_funny_then_fill_and_never_overlap():
    js = [_j(i, length=9.0) for i in range(60)]  # 9 s sentences, 1 s gaps
    moments = [
        _m(0, js[2].start, js[3].end, 9, "funny", True, 2, 3),
        _m(1, js[10].start, js[11].end, 10, "concept", True, 10, 11),
        _m(2, js[20].start, js[21].end, 7, "funny", True, 20, 21),
        _m(3, js[3].start, js[4].end, 8, "funny", True, 3, 4),  # overlaps short 0
        _m(4, js[40].start, js[41].end, 9, "funny", False, 40, 41),  # not short-worthy
    ]
    shorts = select_shorts(moments, js, count=3, min_s=20, max_s=60)
    assert [s.moment_id for s in shorts] == [1, 0, 2]  # ordered by score; funny chosen first, concept fills
    assert [s.index for s in shorts] == [1, 2, 3]
    for s in shorts:
        assert 20 <= s.end - s.start <= 60


def test_short_windows_widen_and_trim_at_sentence_boundaries():
    js = [_j(i, length=9.0) for i in range(30)]
    tiny = _m(0, js[10].start, js[10].end, 9, "funny", True, 10, 10)
    huge = _m(1, js[15].start, js[29].end, 9, "funny", True, 15, 29)
    shorts = select_shorts([tiny, huge], js, count=2, min_s=20, max_s=60)
    starts = {j.start for j in js}
    ends = {j.end for j in js}
    for s in shorts:
        assert s.start in starts and s.end in ends
        assert 20 <= s.end - s.start <= 60


def test_no_shorts_requested_or_available():
    js = [_j(i) for i in range(5)]
    assert select_shorts([_m(0, 0, 10, 9, "funny", True, 0, 1)], js, count=0) == []
    assert select_shorts([_m(0, 0, 10, 9, "funny", False, 0, 1)], js, count=4) == []
    assert select_shorts([_m(0, 0, 10, 3, "funny", True, 0, 1)], js, count=4) == []  # below min score
