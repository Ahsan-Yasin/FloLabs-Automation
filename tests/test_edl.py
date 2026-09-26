import itertools

import pytest

from core.models import Decision, EditDecisionList, EDLRange, Word
from edl.builder import build_edl, complement, validate_edl


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


# ---------------------------------------------------------------- v2 frame grid

def _frames(edl):
    return [(r.start_frame, r.end_frame) for r in edl.ranges]


def test_frame_grid_snaps_every_boundary():
    decisions = [
        Decision(start=1.013, end=4.987, decision="keep"),
        Decision(start=6.02, end=9.51, decision="keep"),
    ]
    edl = build_edl(decisions, [], 12.0, fps="30/1", fade_frames=16)
    assert _frames(edl) == [(30, 150), (181, 285)]
    for r in edl.ranges:
        assert r.start == r.start_frame / 30 and r.end == r.end_frame / 30
    assert edl.fps == "30/1" and edl.fade_frames == 16 and edl.total_frames == 360


def test_gaps_shorter_than_the_dissolve_are_merged_back_and_counted():
    fps = 25
    decisions = [
        Decision(start=0.0, end=4.0, decision="keep"),
        Decision(start=4.4, end=8.0, decision="keep"),  # 10-frame gap < d+1 = 13 -> merged
        Decision(start=10.0, end=14.0, decision="keep"),  # 50-frame gap -> real cut
    ]
    edl = build_edl(decisions, [], 20.0, fps=fps, fade_frames=12)
    assert _frames(edl) == [(0, 200), (250, 350)]
    assert edl.merged_gap_count == 1
    assert edl.merged_gap_seconds == 0.4


def test_short_keep_is_widened_not_merged_across_a_removed_gap():
    """Regression: merging a 0.6 s keep into its neighbour used to pull the
    whole removed gap between them back into the video."""
    decisions = [
        Decision(start=0.0, end=4.0, decision="keep"),
        Decision(start=6.0, end=6.6, decision="keep"),  # 15 frames < 2d = 24
        Decision(start=9.0, end=12.0, decision="keep"),
    ]
    edl = build_edl(decisions, [], 20.0, fps=25, fade_frames=12)
    assert _frames(edl) == [(0, 100), (145, 169), (225, 300)]  # widened by 5 + 4 frames
    assert edl.merged_gap_count == 0


def test_sounds_good_between_two_cut_tangents_stays_short():
    """The live case: 'Sounds good.' (0.76 s) kept, then 7 s of weekend small
    talk removed, then real content. The small talk must stay removed."""
    decisions = [
        Decision(start=36.4, end=43.0, decision="keep"),
        Decision(start=43.5, end=44.26, decision="keep"),
        Decision(start=44.26, end=51.0, decision="remove"),
        Decision(start=51.5, end=55.0, decision="keep"),
    ]
    edl = build_edl(decisions, [], 60.0, fps=25, fade_frames=12)
    kept = [(r.start, r.end) for r in edl.ranges]
    assert all(not (s < 47.0 < e) for s, e in kept), kept  # the middle of the small talk is not kept
    assert any(s <= 43.6 and e >= 44.2 for s, e in kept)  # "Sounds good." survives


def test_short_keep_with_no_room_merges_only_across_a_tiny_gap():
    # 10-frame keep squeezed between 14-frame gaps (d=12 needs gaps >= 13): no
    # room to widen, gap 0.56 s <= 1 s -> merged with a neighbour.
    ranges = [Decision(start=0.0, end=4.0, decision="keep"),
              Decision(start=4.56, end=4.96, decision="keep"),
              Decision(start=5.52, end=9.0, decision="keep")]
    edl = build_edl(ranges, [], 12.0, fps=25, fade_frames=12)
    assert len(edl.ranges) == 2
    assert edl.merged_gap_count == 1


def test_short_keep_is_dropped_when_it_cannot_be_widened_or_merged():
    decisions = [
        Decision(start=0.0, end=10.0, decision="keep"),
        Decision(start=20.0, end=22.0, decision="keep"),  # 2 s < 8 s minimum, big gaps
        Decision(start=30.0, end=40.0, decision="keep"),
    ]
    edl = build_edl(decisions, [], 40.0, fps=25, fade_frames=12, min_segment_s=8.0)
    # widened to 8 s using the room in the 10-s gaps rather than merged
    assert _frames(edl) == [(0, 250), (425, 625), (750, 1000)]
    tight = build_edl(decisions, [], 40.0, fps=25, fade_frames=12, min_segment_s=25.0)
    assert (500, 550) not in _frames(tight)


def test_edge_slivers_shorter_than_half_the_fade_are_kept():
    decisions = [Decision(start=0.1, end=9.9, decision="keep")]  # 3 frames at each edge @30, h=8
    edl = build_edl(decisions, [], 10.0, fps=30, fade_frames=16)
    assert _frames(edl) == [(0, 300)]


def test_ranges_past_the_end_are_dropped_and_ends_clamped():
    decisions = [
        Decision(start=0.0, end=5.0, decision="keep"),
        Decision(start=8.0, end=30.0, decision="keep"),
        Decision(start=40.0, end=45.0, decision="keep"),
    ]
    edl = build_edl(decisions, [], 10.0, fps=25, fade_frames=12)
    assert _frames(edl) == [(0, 125), (200, 250)]


def test_fallback_flag_and_no_fallback_mode():
    edl = build_edl([], [], 10.0, fps=25, fade_frames=12)
    assert edl.used_full_video_fallback and _frames(edl) == [(0, 250)]
    empty = build_edl([], [], 10.0, fps=25, fade_frames=12, full_video_fallback=False)
    assert empty.ranges == []


def test_source_shorter_than_two_dissolves_builds_and_plans():
    """Regression: a 10-frame source with d=16 (< 2d) failed validate_edl, even
    for its own full-video fallback. A lone range has no cut and needs no
    dissolve; a real single keep must not be dropped into the fallback."""
    from slice.plan import plan_video

    fallback = build_edl([], [], 10 / 30, fps=30, fade_frames=16, total_frames=10)
    assert fallback.used_full_video_fallback and _frames(fallback) == [(0, 10)]
    keep = build_edl([Decision(start=0.0, end=10 / 30, decision="keep")], [], 10 / 30, fps=30, fade_frames=16,
                     total_frames=10, min_segment_s=0.1)
    assert not keep.used_full_video_fallback and _frames(keep) == [(0, 10)]
    for edl in (fallback, keep):
        assert sum(p.frames for p in plan_video(_frames(edl), 16)) == 10


def test_lone_short_keep_is_not_widened_but_multiple_keeps_still_need_two_dissolves():
    edl = build_edl([Decision(start=2.0, end=2.6, decision="keep")], [], 10.0, fps=30, fade_frames=16)
    assert _frames(edl) == [(60, 78)]  # 18 frames >= min_segment 0.5 s; no cut, no 2d minimum
    bad = EditDecisionList(
        ranges=[EDLRange(start=0, end=0.6, start_frame=0, end_frame=18),
                EDLRange(start=2, end=4, start_frame=60, end_frame=120)],
        source_duration=10, fps="30/1", fade_frames=16, total_frames=300,
    )
    with pytest.raises(ValueError):
        validate_edl(bad)


def test_complement_tiles_the_source_exactly_with_tiers():
    decisions = [
        Decision(start=2.0, end=5.0, decision="keep"),
        Decision(start=5.6, end=9.0, decision="keep"),  # 0.6 s gap: cut, transcript only
        Decision(start=12.0, end=15.0, decision="keep"),
    ]
    edl = build_edl(decisions, [], 20.0, fps=25, fade_frames=12)
    removed = complement(edl, video_min_gap_s=1.0)
    pieces = sorted(
        [(r.start_frame, r.end_frame, "keep") for r in edl.ranges]
        + [(r.start_frame, r.end_frame, r.tier) for r in removed]
    )
    assert pieces[0][0] == 0 and pieces[-1][1] == 500
    for a, b in itertools.pairwise(pieces):
        assert a[1] == b[0]  # no gap, no overlap
    tiers = {(r.start_frame, r.end_frame): r.tier for r in removed}
    assert tiers == {(0, 50): "video", (125, 140): "transcript_only", (225, 300): "video", (375, 500): "video"}


def test_complement_seconds_mode():
    edl = build_edl([Decision(start=1.0, end=3.0, decision="keep")], [], 5.0)
    assert [(r.start, r.end) for r in complement(edl, 1.0)] == [(0.0, 1.0), (3.0, 5.0)]


def test_frame_validation_rejects_short_gap():
    bad = EditDecisionList(
        ranges=[EDLRange(start=0, end=4, start_frame=0, end_frame=100),
                EDLRange(start=4.2, end=8, start_frame=105, end_frame=200)],
        source_duration=8, fps="25/1", fade_frames=12, total_frames=200,
    )
    with pytest.raises(ValueError):
        validate_edl(bad)


# ---------------------------------------------------------------- silence cuts


def _caption_sentence(start, end):
    """A caption transcript's snap "word" is a whole sentence."""
    return Word(word="a whole caption sentence.", start=start, end=end, speaker="A")


def test_silence_inside_a_kept_sentence_is_cut_after_word_snapping():
    """The owner's bug: a kept caption sentence kept its dead air. Snapping to
    the sentence would stretch any earlier cut back out to its edges. 0.30 s
    is left after the speech and 0.25 s before it resumes (the settings)."""
    words = [_caption_sentence(0.0, 20.0)]
    edl = build_edl([Decision(start=0.0, end=20.0, decision="keep")], words, 20.0, fps=30, fade_frames=16,
                    silences=[(5.0, 10.0)])
    assert _frames(edl) == [(0, 159), (293, 600)]
    assert edl.silence_cuts == [(5.3, 9.75)]
    assert edl.merged_gap_count == 0
    removed = complement(edl)
    assert [(r.start_frame, r.end_frame) for r in removed] == [(159, 293)]  # still tiles the source


def test_silence_cuts_only_touch_kept_ranges_and_too_short_ones_are_dropped():
    decisions = [Decision(start=0.0, end=10.0, decision="keep"), Decision(start=10.0, end=14.0, decision="remove"),
                 Decision(start=14.0, end=30.0, decision="keep")]
    cuts = [(3.0, 3.55),  # 0.55 s < d+2 frames (0.6 s @30 fps): no cut, and not a "merged gap" either
            (8.0, 12.0),  # runs into the removed sentence: only 8-10 is taken from the keep
            (20.0, 22.0)]
    edl = build_edl(decisions, [], 30.0, fps=30, fade_frames=16, silences=cuts, silence_pad_s=(0.0, 0.0))
    assert _frames(edl) == [(0, 240), (420, 600), (660, 900)]
    assert edl.silence_cuts == [(8.0, 10.0), (20.0, 22.0)]
    assert edl.merged_gap_count == 0
    pieces = sorted([(r.start_frame, r.end_frame) for r in edl.ranges]
                    + [(r.start_frame, r.end_frame) for r in complement(edl)])
    assert pieces[0][0] == 0 and pieces[-1][1] == 900
    assert all(a[1] == b[0] for a, b in itertools.pairwise(pieces))


def test_speech_between_two_silence_cuts_is_widened_never_dropped():
    """0.6 s of speech (its paddings) between two long pauses is shorter than
    two dissolves (2d = 32 frames): it is widened a little into the cuts —
    it must neither vanish nor pull the pauses back in."""
    keep = [Decision(start=0.0, end=20.0, decision="keep")]
    edl = build_edl(keep, [], 20.0, fps=30, fade_frames=16, silences=[(2.0, 6.0), (6.6, 10.0)],
                    silence_pad_s=(0.0, 0.0))
    frames = _frames(edl)
    middle = next(f for f in frames if f[0] <= 180 < f[1])  # the speech at 6.0-6.6 s
    assert middle[0] <= 180 and middle[1] >= 198 and middle[1] - middle[0] == 32
    assert len(frames) == 3 and len(edl.silence_cuts) == 2
    assert sum(r.end - r.start for r in complement(edl)) > 6.5  # most of the 7.4 s of pause is still cut


def test_speech_between_two_short_cuts_is_merged_across_one_not_dropped():
    keep = [Decision(start=0.0, end=10.0, decision="keep")]
    edl = build_edl(keep, [], 10.0, fps=30, fade_frames=16, silences=[(2.0, 2.7), (3.3, 4.0)],
                    silence_pad_s=(0.0, 0.0))
    assert any(s <= 81 and e >= 99 for s, e in _frames(edl))  # 2.7-3.3 s kept
    assert len(edl.ranges) == 2 and len(edl.silence_cuts) == 1  # one pause put back instead


def test_silence_cuts_in_the_seconds_builder():
    edl = build_edl([Decision(start=0.0, end=10.0, decision="keep")], [], 10.0, silences=[(4.0, 6.0)],
                    silence_pad_s=(0.0, 0.0))
    assert [(r.start, r.end) for r in edl.ranges] == [(0.0, 4.0), (6.0, 10.0)]


def _kept_frames(edl):
    return {f for s, e in _frames(edl) for f in range(s, e)}


def _filler_around(keep_a, filler, keep_b):
    return [Decision(start=keep_a[0], end=keep_a[1], decision="keep"),
            Decision(start=filler[0], end=filler[1], decision="remove"),
            Decision(start=keep_b[0], end=keep_b[1], decision="keep")]


@pytest.mark.parametrize("silence", [(12.0, 20.1),  # the pause runs past the kept sentence's end
                                     (12.0, 19.9)])  # the pause ends 0.1 s before it (caption timing)
def test_a_pause_at_the_end_of_a_kept_sentence_never_pulls_the_removed_one_after_it_back(silence):
    """The owner's b22e at 18:35: a kept sentence ends in a pause that runs
    into a removed one. Padded on both sides, the cut left a 0.2 s sliver of
    dead air at the keep's end, and the short-keep rule widened it to 2d
    frames half INTO the removed sentence: a 1 s flash of dead air plus its
    first word between two dissolves. The pad before speech is only for
    speech that stays; a too-short edge piece grows into its own pause."""
    decisions = _filler_around((0.0, 20.0), (20.0, 22.0), (22.0, 40.0))
    words = [_caption_sentence(0.0, 20.0), _caption_sentence(20.0, 22.0), _caption_sentence(22.0, 40.0)]
    base = build_edl(decisions, words, 40.0, fps=30, fade_frames=16)
    edl = build_edl(decisions, words, 40.0, fps=30, fade_frames=16, silences=[silence])
    assert _kept_frames(edl) <= _kept_frames(base)  # nothing the decisions removed comes back
    assert not _kept_frames(edl) & set(range(600, 660))  # the filler (20-22 s)
    assert _frames(edl)[0][0] == 0 and _frames(edl)[0][1] == 369  # 0.30 s after "A" stops at 12.0
    if silence[1] > 20.0:
        assert _frames(edl) == [(0, 369), (660, 1200)] and edl.silence_cuts == [(12.3, 20.0)]
    else:  # the 0.35 s left before the filler grows into the pause (to 2d + 1 frames), not the filler
        assert _frames(edl) == [(0, 369), (567, 600), (660, 1200)]


def test_a_pause_at_the_start_of_a_kept_sentence_is_cut_to_its_edge():
    decisions = _filler_around((0.0, 8.0), (8.0, 10.0), (10.0, 30.0))
    base = _kept_frames(build_edl(decisions, [], 30.0, fps=30, fade_frames=16))
    # starts 0.1 s before the keep, in the removed filler: padded, 10.0-10.2 was left and widened into the filler
    edl = build_edl(decisions, [], 30.0, fps=30, fade_frames=16, silences=[(9.9, 17.25)])
    assert _frames(edl) == [(0, 240), (510, 900)] and edl.silence_cuts == [(10.0, 17.0)]
    # starts 0.1 s after the keep does: that piece (0.4 s with its padding) grows into its pause to 2d + 1 frames
    edl = build_edl(decisions, [], 30.0, fps=30, fade_frames=16, silences=[(10.1, 17.25)])
    assert _frames(edl) == [(0, 240), (300, 333), (510, 900)] and edl.silence_cuts == [(11.1, 17.0)]
    assert _kept_frames(edl) <= base
