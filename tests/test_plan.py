import random

import pytest

from slice.plan import PlanError, output_offsets, plan_video, split_into_atoms


def _total(ranges):
    return sum(e - s for s, e in ranges)


def test_single_range_is_one_plain_part():
    parts = plan_video([(10, 500)], 16)
    assert len(parts) == 1 and parts[0].join == "none"
    assert parts[0].inputs[0].start == 10 and parts[0].inputs[0].end == 500
    assert parts[0].frames == 490


def test_dissolves_extend_half_the_fade_into_the_removed_gap():
    parts = plan_video([(0, 100), (150, 250)], 12)
    (batch,) = parts
    assert [(s.start, s.end) for s in batch.inputs] == [(0, 106), (144, 250)]
    assert batch.frames == 200
    assert batch.xfade_offsets() == [94]  # dissolve centred on output frame 100


def test_batches_are_joined_by_seam_pieces():
    ranges = [(0, 100), (150, 250), (300, 400), (450, 550)]
    parts = plan_video(ranges, 12, batch_size=2)
    assert [p.kind for p in parts] == ["batch", "seam", "batch"]
    first, seam, second = parts
    assert [(s.start, s.end) for s in first.inputs] == [(0, 106), (144, 244)]
    assert [(s.start, s.end) for s in seam.inputs] == [(244, 256), (294, 306)]
    assert [(s.start, s.end) for s in second.inputs] == [(306, 406), (444, 550)]
    assert sum(p.frames for p in parts) == _total(ranges)


def test_long_ranges_split_at_hard_boundaries():
    atoms = split_into_atoms([(0, 1000)], 300)
    assert [(a.start, a.end) for a in atoms] == [(0, 250), (250, 500), (500, 750), (750, 1000)]
    parts = plan_video([(0, 1000), (1100, 1200)], 16, batch_size=20, max_batch_frames=300)
    # hard splits inside the long range need no seam; the cut to the next
    # range (which doesn't fit in the same batch) gets one
    assert [p.kind for p in parts] == ["batch", "batch", "batch", "batch", "seam", "batch"]
    assert [(s.start, s.end) for s in parts[3].inputs] == [(750, 992)]
    assert [(s.start, s.end) for s in parts[4].inputs] == [(992, 1008), (1092, 1108)]
    assert sum(p.frames for p in parts) == 1100


def test_hard_cut_mode_uses_concat():
    parts = plan_video([(0, 50), (60, 90)], 0)
    assert parts[0].join == "concat" and parts[0].frames == 80


@pytest.mark.parametrize("seed", range(20))
def test_random_plans_always_sum_exactly(seed):
    rng = random.Random(seed)
    d = rng.choice([0, 12, 14, 16, 30])
    ranges, cursor = [], rng.randint(0, 50)
    for _ in range(rng.randint(1, 60)):
        length = rng.randint(max(2 * d, 1), 3000)
        ranges.append((cursor, cursor + length))
        cursor += length + rng.randint(d + 1, 400)
    parts = plan_video(ranges, d, batch_size=rng.randint(1, 20), max_batch_frames=rng.choice([0, 900, 5000]))
    assert sum(p.frames for p in parts) == _total(ranges)
    for p in parts:
        assert all(s.frames > 0 for s in p.inputs)
        if p.join == "xfade":
            assert all(s.frames >= d for s in p.inputs)


def test_rejects_invalid_ranges():
    with pytest.raises(PlanError):
        plan_video([], 12)
    with pytest.raises(PlanError):
        plan_video([(0, 100), (105, 200)], 12)  # gap shorter than the dissolve
    with pytest.raises(PlanError):
        plan_video([(0, 100)], 13)  # odd fade


def test_lone_range_shorter_than_the_dissolve_plans_fine():
    """Regression: a 10-frame clip (d=16) could not render at all, not even as
    the full-video fallback. A lone range has no cut, so no dissolve."""
    (part,) = plan_video([(0, 10)], 16)
    assert part.join == "none" and part.frames == 10
    with pytest.raises(PlanError):
        plan_video([(0, 10), (40, 100)], 16)  # with a cut, every range still needs d frames


def test_output_offsets():
    assert output_offsets([(10, 20), (30, 45), (50, 51)]) == [0, 10, 25]
