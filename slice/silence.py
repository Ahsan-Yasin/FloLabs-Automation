"""Find the stretches of a recording where nobody is speaking — from the
audio itself.

Transcript timings can't show pauses: a caption cue stays on screen until
the next one starts (transcribe/native.py clips each cue's end to the next
cue's start), so a caption transcript has no gaps, and a kept caption
"sentence" keeps its dead air. On the owner's 24-minute YouTube meeting 95 s
of the 119 s of silence left in final.mp4 sat inside kept sentences, where
neither the LLM (text only) nor the EDL (sentence-level timings) could see
it. So the source audio is decoded once to 16 kHz mono and measured in 50 ms
windows; every run of windows quieter than the recording's threshold that
lasts at least `min_s` is a silence.

The threshold adapts to each recording, because no fixed level works on
both a clean, noise-suppressed Zoom call (pauses at -75 dBFS, speech at -20)
and a quiet, noisy room (room tone at -57, speech at -34, one talker at -45):

    floor = p5 of the window levels (room tone), speech = p95, R = speech - floor
    talk  = median of the windows >= floor + 10 dB (typical speech)
    threshold = min(floor + min(max(min_margin_db, range_fraction * R), R - speech_headroom_db),
                    talk - TALK_GAP_DB)

— 35% of the range, but never within `speech_headroom_db` of loud speech or
TALK_GAP_DB of typical speech (so a quiet talker is not "silence"). If that
leaves less than `min_margin_db` above room tone, a pause can't be told from
a quiet talker and nothing is cut. The statistics come from the part of the
recording the transcript covers (an intro jingle or a silent wait before the
meeting says nothing about its talkers) and leave out digital silence (a
muted track or noise gate would drag the floor below the real room tone;
only when there is no room tone left at all is the floor -80 dBFS); every
window, zeros included, can still be part of a silence.

Short blips (a mouse click) are NOT bridged: every window of a cut is
below the threshold. ffmpeg's silencedetect is not used: it is a per-sample
peak test at a fixed level, and one click ends a silence.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from core.config import Settings
from core.logging import get_logger
from core.proc import stream_checked

from .ffmpeg_wrapper import ffmpeg_bin
from .profile import BASE_ARGS, ffmpeg_timeout

logger = get_logger(__name__)

SAMPLE_RATE = 16000
WINDOW_S = 0.05
WINDOW_SAMPLES = round(SAMPLE_RATE * WINDOW_S)
# decoded in blocks of this many seconds, so a 3-hour meeting never sits in memory
BLOCK_S = 60
FLOOR_PERCENTILE, SPEECH_PERCENTILE = 5, 95
# the level of an all-zero window
DIGITAL_SILENCE_DB = -120.0
# A muted track, a noise gate or Opus' silence suppression gives (near)
# digital zeros. Real room tone is never quieter than this, so quieter windows
# are left out of the statistics: in more than 5% of the file they would put
# p5 at -120 dB, and even clamping it here put the threshold below a noisy
# room's real room tone (no pause found at all).
MIN_FLOOR_DB = -80.0
# Windows this far above room tone are someone talking (or other sound);
# their median is the typical speech level.
ACTIVE_DB = 10.0
# The threshold stays this far below typical speech. p95 alone is not enough:
# anything louder than the talkers in >= 5% of the recording (a video played
# over screen share, a hot mic) becomes "speech" and lifts the threshold into
# the quiet talkers; the median of the active windows barely moves.
TALK_GAP_DB = 10.0
# bump when the detection itself changes, so cached results are redone
DETECTION_VERSION = 2


@dataclass(frozen=True)
class SilenceParams:
    min_s: float = 1.0
    min_margin_db: float = 6.0
    range_fraction: float = 0.35
    speech_headroom_db: float = 18.0

    @classmethod
    def from_settings(cls, s: Settings) -> SilenceParams:
        return cls(min_s=s.silence_min_s, min_margin_db=s.silence_min_margin_db,
                   range_fraction=s.silence_range_fraction, speech_headroom_db=s.silence_speech_headroom_db)


@dataclass
class SilenceMap:
    """Silences of one recording (source seconds) and how they were found."""

    ranges: list[tuple[float, float]] = field(default_factory=list)
    threshold_db: float | None = None
    floor_db: float | None = None
    speech_db: float | None = None
    talk_db: float | None = None
    audio_s: float = 0.0
    # why nothing was detected (e.g. no usable dynamic range), for the log
    note: str = ""

    @property
    def total_s(self) -> float:
        return sum(e - s for s, e in self.ranges)


def window_levels(source: Path, duration_s: float = 0.0) -> np.ndarray:
    """RMS level (dBFS) of every 50 ms window of the source's audio, decoded
    as 16 kHz mono PCM and streamed through a pipe. Same stream choice and
    aresample as the renderer's FLAC (slice/ffmpeg_wrapper.extract_audio_flac),
    so times line up with what gets cut."""
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-i", str(source), "-vn", "-sn", "-dn",
           "-af", f"aresample=async=1:first_pts=0,aresample={SAMPLE_RATE}", "-ac", "1", "-ar", str(SAMPLE_RATE),
           "-c:a", "pcm_s16le", "-f", "s16le", "-"]
    levels: list[np.ndarray] = []
    carry = b""
    step = WINDOW_SAMPLES * 2
    for chunk in stream_checked(cmd, BLOCK_S * SAMPLE_RATE * 2, timeout=ffmpeg_timeout(duration_s / 10.0)):
        buf = carry + chunk
        usable = len(buf) - len(buf) % step
        carry = buf[usable:]
        if usable:
            levels.append(_db(np.frombuffer(buf[:usable], dtype="<i2").reshape(-1, WINDOW_SAMPLES)))
    tail = len(carry) - len(carry) % 2
    if tail:  # a last, shorter window
        levels.append(_db(np.frombuffer(carry[:tail], dtype="<i2").reshape(1, -1)))
    return np.concatenate(levels) if levels else np.zeros(0)


def _db(windows: np.ndarray) -> np.ndarray:
    x = windows.astype(np.float64) / 32768.0
    rms = np.sqrt(np.mean(x * x, axis=1))
    return 20.0 * np.log10(np.maximum(rms, 10 ** (DIGITAL_SILENCE_DB / 20.0)))


def adaptive_threshold(levels: np.ndarray, params: SilenceParams) -> tuple[float, float, float, float]:
    """(threshold, floor, speech, talk) in dBFS from these window levels (see
    the module docstring). Speech is never cut to satisfy the minimum margin:
    a threshold less than `min_margin_db` above the floor means nothing can
    be cut (detect_silences checks)."""
    audible = levels[levels >= MIN_FLOOR_DB]
    if not len(audible):  # all digital silence
        return MIN_FLOOR_DB, MIN_FLOOR_DB, MIN_FLOOR_DB, MIN_FLOOR_DB
    speech = float(np.percentile(audible, SPEECH_PERCENTILE))

    def rule(floor: float) -> tuple[float, float, float, float]:
        active = audible[audible >= floor + ACTIVE_DB]
        talk = float(np.median(active)) if len(active) else speech
        r = speech - floor
        margin = min(max(params.min_margin_db, params.range_fraction * r), r - params.speech_headroom_db)
        return min(floor + margin, talk - TALK_GAP_DB), floor, speech, talk

    found = rule(float(np.percentile(audible, FLOOR_PERCENTILE)))
    if not can_cut(found[0], found[1], params) and len(audible) < 0.95 * len(levels):
        # A noise gate with no room tone left at all: the audible windows are
        # all speech, and the digital silence (>= 5% of the windows) is the
        # pauses. Measure from MIN_FLOOR_DB instead.
        found = rule(MIN_FLOOR_DB)
    return found


def can_cut(threshold: float, floor: float, params: SilenceParams) -> bool:
    """The threshold is at least `min_margin_db` above room tone, so room
    tone's own ups and downs are not taken for speech (float noise aside)."""
    return threshold - floor >= params.min_margin_db - 1e-9


def silent_runs(levels: np.ndarray, threshold_db: float, min_s: float,
                window_s: float = WINDOW_S) -> list[tuple[float, float]]:
    """(start, end) seconds of every run of consecutive windows below the
    threshold that lasts at least `min_s`. No bridging: one window at or
    above the threshold ends a run."""
    if not len(levels):
        return []
    below = np.concatenate(([0], (levels < threshold_db).astype(np.int8), [0]))
    edges = np.diff(below)
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    min_windows = min_s / window_s - 1e-9
    return [(round(float(a * window_s), 3), round(float(b * window_s), 3))
            for a, b in zip(starts, ends, strict=True) if b - a >= min_windows]


def detect_silences(source: Path, params: SilenceParams, duration_s: float = 0.0,
                    span: tuple[float, float] | None = None) -> SilenceMap:
    """Measure the source and find its silences (no cache). The threshold's
    statistics come from `span` (source seconds, the part the transcript
    covers) when given; silences are found anywhere."""
    levels = window_levels(source, duration_s)
    audio_s = len(levels) * WINDOW_S
    if not len(levels):
        return SilenceMap(audio_s=0.0, note="no audio decoded")
    stats = levels
    if span is not None:
        stats = levels[max(0, int(span[0] / WINDOW_S)):max(0, int(np.ceil(span[1] / WINDOW_S)))]
        if not len(stats):  # a transcript past the end of the audio: use it all
            stats = levels
    threshold, floor, speech, talk = adaptive_threshold(stats, params)
    found = SilenceMap(threshold_db=round(threshold, 2), floor_db=round(floor, 2), speech_db=round(speech, 2),
                       talk_db=round(talk, 2), audio_s=round(audio_s, 3))
    if not can_cut(threshold, floor, params):
        found.note =(f"room tone ({floor:.0f} dBFS) is too close to the speech ({talk:.0f} dBFS typical, "
                      f"{speech:.0f} loud) to tell a pause from a quiet talker; nothing is cut")
        return found
    found.ranges = silent_runs(levels, threshold, params.min_s)
    return found


def find_silences(source: Path, cache_path: Path, params: SilenceParams, duration_s: float = 0.0,
                  span: tuple[float, float] | None = None) -> SilenceMap:
    """detect_silences, cached in `cache_path` (the job's silences.json) under
    the source file's size + mtime, the parameters and the span, so a
    re-render (or rendering a decide_only job) doesn't decode the recording
    again."""
    st = source.stat()
    key = {"source_bytes": st.st_size, "source_mtime_ns": st.st_mtime_ns, "version": DETECTION_VERSION,
           "window_s": WINDOW_S, "span": None if span is None else [round(span[0], 3), round(span[1], 3)],
           **asdict(params)}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if data.get("key") == key:
            return SilenceMap(ranges=[(s, e) for s, e in data["silences"]], threshold_db=data["threshold_db"],
                              floor_db=data["floor_db"], speech_db=data["speech_db"], talk_db=data.get("talk_db"),
                              audio_s=data["audio_s"], note=data.get("note", ""))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    found = detect_silences(source, params, duration_s, span)
    try:
        cache_path.write_text(json.dumps({
            "key": key,
            "threshold_db": found.threshold_db,
            "floor_db": found.floor_db,
            "speech_db": found.speech_db,
            "talk_db": found.talk_db,
            "audio_s": found.audio_s,
            "note": found.note,
            "total_s": round(found.total_s, 3),
            "silences": [[s, e] for s, e in found.ranges],
        }, indent=1), encoding="utf-8")
    except OSError as exc:  # only the cache is lost; the result is still good
        logger.warning("could not save %s: %s", cache_path, exc)
    return found


def reach_end(silences: list[tuple[float, float]], audio_s: float, end_s: float) -> list[tuple[float, float]]:
    """A silence that runs to the end of the audio runs to the end of the
    video (`end_s`): the video can be a little longer than its audio, and
    nobody speaks after the audio stops."""
    if silences and silences[-1][1] >= audio_s - WINDOW_S and end_s > silences[-1][1]:
        return [*silences[:-1], (silences[-1][0], end_s)]
    return list(silences)


def trim_to_speech(start: float, end: float, silences: list[tuple[float, float]], pad_after_s: float,
                   pad_before_s: float) -> tuple[float, float]:
    """Pull a single window's edges (a short) out of the silences they sit in,
    down to the padding. Nothing inside the window changes."""
    new_start, new_end = start, end
    for s, e in silences:
        if s <= start < e:
            new_start = max(start, e - pad_before_s)
        if s < end <= e:
            new_end = min(end, s + pad_after_s)
    if new_end - new_start < 1.0:  # the window is (nearly) all silence: leave it alone
        return start, end
    return new_start, new_end
