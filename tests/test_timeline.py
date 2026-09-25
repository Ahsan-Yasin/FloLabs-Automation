import itertools
from fractions import Fraction

import pytest

from core.timeline import (
    fade_frames_for,
    fmt_clock,
    parse_rate,
    samples_at_frame,
    to_frame,
    ts,
)

RATES = {
    "24/1": 12,
    "25/1": 12,
    "30000/1001": 14,
    "30/1": 16,
    "60/1": 30,
}


@pytest.mark.parametrize("rate,frames", RATES.items())
def test_fade_is_even_and_near_half_a_second(rate, frames):
    fps = parse_rate(rate)
    d = fade_frames_for(fps, 0.5)
    assert d == frames
    assert d % 2 == 0
    assert abs(float(d / fps) - 0.5) < 1 / float(fps)


def test_fade_disabled_and_minimum():
    assert fade_frames_for(Fraction(30), 0) == 0
    assert fade_frames_for(Fraction(30), 0.01) == 2


@pytest.mark.parametrize("rate", RATES)
def test_frame_boundaries_round_trip(rate):
    fps = parse_rate(rate)
    for n in (0, 1, 7, 1799, 177322):
        assert to_frame(n / fps, fps) == n
        assert to_frame(float(n / fps), fps) == n


@pytest.mark.parametrize("rate", RATES)
def test_cumulative_audio_samples_are_exact(rate):
    """Rounding cumulative boundaries (not per-piece lengths) keeps the total
    exact even when a frame is not a whole number of samples (29.97 fps)."""
    fps = parse_rate(rate)
    lengths = [17, 301, 45, 1000, 3]
    bounds = [0]
    for n in lengths:
        bounds.append(bounds[-1] + n)
    samples = [samples_at_frame(b, fps) for b in bounds]
    pieces = [b - a for a, b in itertools.pairwise(samples)]
    assert sum(pieces) == samples_at_frame(sum(lengths), fps)
    assert all(abs(p - n * 48000 / float(fps)) <= 1 for p, n in zip(pieces, lengths))


def test_parse_rate_forms():
    assert parse_rate("30000/1001") == Fraction(30000, 1001)
    assert parse_rate("25") == 25
    assert parse_rate(29.97) == Fraction(2997, 100)
    with pytest.raises(ValueError):
        parse_rate("0/0")
    with pytest.raises(ValueError):
        parse_rate("0")


def test_ts_formatting_is_fixed_point():
    assert ts(Fraction(59, 60)) == "0.983333"
    assert ts(Fraction(1, 3)) == "0.333333"
    assert ts(Fraction(2, 3)) == "0.666667"
    assert ts(12) == "12.000000"


def test_clock_labels():
    assert fmt_clock(70.9) == "01:10"
    assert fmt_clock(3723) == "1:02:03"
    assert fmt_clock(5, force_hours=True) == "0:00:05"
