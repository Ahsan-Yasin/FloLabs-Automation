import pytest

from core.models import Decision, EditDecisionList, EDLRange, Word
from edl.builder import build_edl, validate_edl


def test_no_crosstalk_is_a_safe_no_op():
    decisions = [Decision(start=0.0, end=10.0, decision="keep")]
    edl = build_edl(decisions, words=[], source_duration=10.0)
    assert len(edl.ranges) == 1
    assert edl.ranges[0].start == 0.0
    assert edl.ranges[0].end == 10.0


def test_removes_crosstalk_between_keeps():
    decisions = [
        Decision(start=0.0, end=5.0, decision="keep"),
        Decision(start=5.0, end=8.0, decision="remove"),
        Decision(start=8.0, end=15.0, decision="keep"),
    ]
    edl = build_edl(decisions, words=[], source_duration=15.0)
    assert [(r.start, r.end) for r in edl.ranges] == [(0.0, 5.0), (8.0, 15.0)]


def test_deoverlaps_and_sorts_untrusted_llm_ordering():
    decisions = [
        Decision(start=8.0, end=15.0, decision="keep"),
        Decision(start=0.0, end=6.0, decision="keep"),  # out of order, overlaps next
        Decision(start=5.0, end=9.0, decision="keep"),
    ]
    edl = build_edl(decisions, words=[], source_duration=20.0)
    validate_edl(edl)  # must not raise: sorted, non-overlapping
    assert len(edl.ranges) == 1
    assert edl.ranges[0].start == 0.0
    assert edl.ranges[0].end == 15.0


def test_snapping_expands_outward_never_clips_a_word():
    words = [
        Word(word="a", start=0.0, end=1.0, speaker="A"),
        Word(word="b", start=1.0, end=2.0, speaker="A"),
        Word(word="c", start=2.0, end=3.0, speaker="B"),
    ]
    decisions = [Decision(start=0.5, end=2.5, decision="keep")]  # cuts mid-word both ends
    edl = build_edl(decisions, words=words, source_duration=3.0)
    assert edl.ranges[0].start == 0.0
    assert edl.ranges[0].end == 3.0


def test_merges_keep_ranges_separated_by_small_gap():
    decisions = [
        Decision(start=0.0, end=5.0, decision="keep"),
        Decision(start=5.1, end=10.0, decision="keep"),  # 0.1s gap < 0.3s threshold
    ]
    edl = build_edl(decisions, words=[], source_duration=10.0)
    assert len(edl.ranges) == 1
    assert edl.ranges[0].start == 0.0
    assert edl.ranges[0].end == 10.0


def test_tiny_segment_merges_into_neighbor():
    decisions = [
        Decision(start=0.0, end=5.0, decision="keep"),
        Decision(start=5.5, end=5.9, decision="keep"),  # 0.4s, below 0.5s min, gap 0.5s (no gap-merge)
        Decision(start=10.0, end=15.0, decision="keep"),
    ]
    edl = build_edl(decisions, words=[], source_duration=15.0)
    assert [(r.start, r.end) for r in edl.ranges] == [(0.0, 5.0), (5.5, 15.0)]


def test_isolated_tiny_segment_dropped_and_falls_back_to_full_video():
    decisions = [Decision(start=1.0, end=1.2, decision="keep")]  # 0.2s, isolated
    edl = build_edl(decisions, words=[], source_duration=20.0)
    assert len(edl.ranges) == 1
    assert edl.ranges[0].start == 0.0
    assert edl.ranges[0].end == 20.0


def test_validate_edl_rejects_overlapping_ranges():
    bad = EditDecisionList(ranges=[EDLRange(start=0, end=5), EDLRange(start=3, end=8)], source_duration=8)
    with pytest.raises(ValueError):
        validate_edl(bad)


def test_validate_edl_rejects_out_of_order_ranges():
    bad = EditDecisionList(ranges=[EDLRange(start=5, end=8), EDLRange(start=0, end=4)], source_duration=8)
    with pytest.raises(ValueError):
        validate_edl(bad)
