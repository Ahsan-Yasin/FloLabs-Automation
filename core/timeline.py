"""Frame-grid arithmetic shared by the EDL builder, the renderer and the remap.

Every cut point in v2 is an integer frame index on the SOURCE's frame grid
(`frame n` starts at n/F seconds from the start of the file). Doing the
arithmetic in whole frames — and only converting to seconds at the edge, for
ffmpeg arguments — is what makes the rendered output land exactly on
Σ(kept frames): any fractional cut point, fade length or extension makes
ffmpeg round each graph up by a frame and the timeline drifts (plan §6.2).
"""

from __future__ import annotations

import math
from fractions import Fraction

AUDIO_RATE = 48000


def parse_rate(value: str | float | Fraction) -> Fraction:
    """'30000/1001' / '25' / 29.97 -> Fraction. Raises ValueError for 0 or junk."""
    if isinstance(value, Fraction):
        rate = value
    elif isinstance(value, int):
        rate = Fraction(value)
    elif isinstance(value, float):
        rate = Fraction(value).limit_denominator(1001)
    else:
        text = str(value).strip()
        if "/" in text:
            num, _, den = text.partition("/")
            if int(den) == 0:
                raise ValueError(f"invalid frame rate {value!r}")
            rate = Fraction(int(num), int(den))
        else:
            rate = Fraction(text).limit_denominator(1001)
    if rate <= 0:
        raise ValueError(f"invalid frame rate {value!r}")
    return rate


def round_half_up(x: Fraction | float) -> int:
    return math.floor(Fraction(x) + Fraction(1, 2))


def fade_frames_for(fps: Fraction, target_s: float = 0.5) -> int:
    """Dissolve length in frames: the even count nearest `target_s`, so h = d/2
    is whole. 0.5 s -> 12 @24, 12 @25, 14 @29.97, 16 @30, 30 @60. Minimum 2."""
    if target_s <= 0:
        return 0
    half = round_half_up(Fraction(fps) * Fraction(target_s).limit_denominator(1000) / 2)
    return max(2, 2 * half)


def to_frame(t: float | Fraction, fps: Fraction) -> int:
    """Nearest frame boundary to time t (seconds)."""
    return round_half_up(Fraction(t) * fps)


def frame_time(n: int, fps: Fraction) -> Fraction:
    return Fraction(n) / fps


def frame_seconds(n: int, fps: Fraction) -> float:
    return float(Fraction(n) / fps)


def samples_at_frame(n: int, fps: Fraction, rate: int = AUDIO_RATE) -> int:
    """Audio sample index aligned with video frame boundary n. Rounding the
    cumulative boundary (never per-piece lengths) keeps the total exact even
    when a frame is not a whole number of samples (29.97 fps: 1601.6)."""
    return round_half_up(Fraction(n) * rate / fps)


def ts(x: Fraction | float, places: int = 6) -> str:
    """Seconds as a fixed-point string for ffmpeg (-ss/-t/xfade offsets)."""
    q = Fraction(x)
    scale = 10**places
    scaled = round_half_up(q * scale)
    sign = "-" if scaled < 0 else ""
    scaled = abs(scaled)
    return f"{sign}{scaled // scale}.{scaled % scale:0{places}d}"


def fmt_clock(seconds: float, force_hours: bool = False) -> str:
    """01:10 / 1:02:03 style label (floor to whole seconds)."""
    total = max(0, math.floor(seconds + 1e-9))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h or force_hours:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def rate_str(fps: Fraction) -> str:
    return f"{fps.numerator}/{fps.denominator}"
