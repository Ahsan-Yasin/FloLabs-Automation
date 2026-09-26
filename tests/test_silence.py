"""Silence detection from the audio (slice/silence.py).

The real-audio tests build small WAVs with ffmpeg's lavfi: a 300 Hz tone for
speech, anoisesrc for room tone, and levels copied from the owner's two
YouTube meetings — a clean, noise-suppressed Zoom call (pauses near digital
silence, speech about -20 to -34 dBFS) and a quiet, noisy room (room tone
about -58, speech -34, one quiet talker at -47) where any fixed level that
finds the pauses also cuts the quiet talker.
"""

import os
import shutil
import subprocess

import numpy as np
import pytest

import slice.silence as silence_module
from slice.silence import (
    SilenceMap,
    SilenceParams,
    adaptive_threshold,
    detect_silences,
    find_silences,
    reach_end,
    silent_runs,
    trim_to_speech,
)

# white room tone of this amplitude measures ~-63 dBFS (in the 16 kHz
# analysis: white noise loses its top two thirds); 0.004 is ~-57.5, f267's
ROOM_TONE_AMP = 0.0022
F267_ROOM_TONE_AMP = 0.004

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg on PATH")


def _make_wav(path, duration, loud=(), quiet=(), noise_amp=0.0, clicks=(), blast=(), mid=(), quiet_amp=0.006):
    """`loud` spans at ~-33.5 dBFS, `mid` at ~-40.5, `quiet` at ~-47.5 (or
    `quiet_amp`), 20 ms clicks at ~-9, `blast` (music louder than anyone
    talks) at ~-15, over digital silence or white room tone of `noise_amp`."""
    def spans(ranges):
        return "+".join(f"between(t,{a},{b})" for a, b in ranges) or "0"

    tone = "sin(2*PI*300*t)"
    expr = (f"0.03*{tone}*({spans(loud)})+0.0134*{tone}*({spans(mid)})+{quiet_amp}*{tone}*({spans(quiet)})"
            f"+0.5*{tone}*({spans([(c, c + 0.02) for c in clicks])})"
            f"+0.25*sin(2*PI*440*t)*({spans(blast)})")
    inputs = ["-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=48000:d={duration}"]
    graph = "[0:a]anull[out]"
    if noise_amp:
        inputs += ["-f", "lavfi", "-i", f"anoisesrc=d={duration}:c=white:a={noise_amp}:r=48000:s=7"]
        graph = "[0:a][1:a]amix=inputs=2:normalize=0[out]"
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "[out]",
                    "-c:a", "pcm_s16le", str(path)], check=True)
    return path


def _overlap(a, b):
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


# ------------------------------------------------------------ real audio


@needs_ffmpeg
def test_quiet_talker_in_a_noisy_room_is_not_cut_but_the_pauses_are(tmp_path):
    src = _make_wav(tmp_path / "room.wav", 20, loud=[(0, 4), (11.3, 15), (18, 20)], quiet=[(6.5, 10.5)],
                    noise_amp=ROOM_TONE_AMP)
    found = detect_silences(src, SilenceParams(), 20)
    # a fixed -45 dBFS would call the quiet talker (~-47) silence; the rule
    # puts the line between room tone and the quietest speech
    assert found.floor_db + 6 <= found.threshold_db < -48
    assert found.speech_db - found.threshold_db >= 18
    assert found.ranges == [pytest.approx((4.0, 6.5), abs=0.06), pytest.approx((15.0, 18.0), abs=0.06)]
    assert all(_overlap(r, (6.5, 10.5)) == 0 for r in found.ranges)  # the quiet talker stays
    # the 0.8 s turn gap (10.5-11.3) is shorter than SILENCE_MIN_S: left alone


@needs_ffmpeg
def test_a_noisier_room_is_not_cut_at_all_rather_than_cut_into_the_quiet_talker(tmp_path):
    """A louder fan (room tone -51 dBFS, 18 dB under the loud talker): no
    level is both 6 dB above room tone and 18 dB under loud speech. The old
    rule let the 6 dB minimum win (-45 dBFS) and cut the quiet talker
    (-47.5 + fan = ~-46) out with the pauses around her."""
    src = _make_wav(tmp_path / "fan.wav", 20, loud=[(0, 4), (11.3, 15), (18, 20)], quiet=[(6.5, 10.5)],
                    noise_amp=0.0087)
    found = detect_silences(src, SilenceParams(), 20)
    assert found.ranges == [] and "nothing is cut" in found.note
    assert found.speech_db - found.threshold_db >= 18


@needs_ffmpeg
def test_music_louder_than_the_talkers_does_not_lift_the_threshold_into_quiet_speech(tmp_path):
    """A video played over screen share for 15% of the meeting (-15 dBFS) in
    f267's room: p95 is the music now, and 35% of that range put the line at
    -43 dBFS, above the quiet talker (~-46): both her turns were cut. Typical
    speech (the median of the active windows: three talkers, loud / mid /
    quiet) caps it 10 dB lower."""
    quiet = [(10.5, 13.5), (26, 29)]  # 0.5 s turn gaps before them
    src = _make_wav(tmp_path / "share.wav", 40, loud=[(0, 3), (17, 20)], mid=[(5, 10), (20.5, 25.5)], quiet=quiet,
                    quiet_amp=0.0071, noise_amp=F267_ROOM_TONE_AMP, blast=[(32, 38)])
    found = detect_silences(src, SilenceParams(), 40)
    assert found.speech_db > -20  # the music
    assert found.threshold_db == pytest.approx(found.talk_db - silence_module.TALK_GAP_DB) and found.talk_db < -39
    assert found.ranges == [pytest.approx(r, abs=0.06) for r in ((3.0, 5.0), (13.5, 17.0), (29.0, 32.0),
                                                                  (38.0, 40.0))]
    assert all(_overlap(r, q) == 0 for r in found.ranges for q in quiet)  # the quiet talker stays


@needs_ffmpeg
def test_statistics_come_from_the_part_the_transcript_covers(tmp_path):
    """A loud intro jingle before the first caption (30% of the file) says
    nothing about the talkers: measured over the transcript's span, the
    threshold is the one of the meeting alone."""
    loud, quiet = [(0, 4), (11.3, 15), (18, 20)], [(6.5, 10.5)]
    alone = detect_silences(_make_wav(tmp_path / "meeting.wav", 20, loud=loud, quiet=quiet, noise_amp=ROOM_TONE_AMP),
                            SilenceParams(), 20)

    def later(spans):
        return [(a + 9, b + 9) for a, b in spans]

    src = _make_wav(tmp_path / "jingle.wav", 29, loud=later(loud), quiet=later(quiet), noise_amp=ROOM_TONE_AMP,
                    blast=[(0, 8.5)])
    whole = detect_silences(src, SilenceParams(), 29)
    spanned = detect_silences(src, SilenceParams(), 29, span=(9.0, 29.0))
    assert whole.speech_db > -20 and whole.threshold_db > alone.threshold_db + 1
    assert spanned.threshold_db == pytest.approx(alone.threshold_db, abs=0.5)
    assert [r for r in spanned.ranges if r[0] >= 9] == [pytest.approx((13.0, 15.5), abs=0.06),
                                                        pytest.approx((24.0, 27.0), abs=0.06)]


@needs_ffmpeg
def test_a_click_ends_a_silence_nothing_is_bridged(tmp_path):
    """Clean, noise-gated call: the pauses are digital silence and there is
    no room tone at all, so the floor is -80 dB, and a mouse click in a pause
    splits it."""
    src = _make_wav(tmp_path / "zoom.wav", 16, loud=[(0, 3), (5, 9), (13.5, 16)], clicks=[10.9])
    found = detect_silences(src, SilenceParams(), 16)
    assert found.floor_db == silence_module.MIN_FLOOR_DB
    assert found.ranges == [pytest.approx((3.0, 5.0), abs=0.06), pytest.approx((9.0, 10.9), abs=0.06),
                            pytest.approx((10.95, 13.5), abs=0.06)]


@needs_ffmpeg
def test_digital_silence_does_not_hide_a_noisy_rooms_pauses(tmp_path):
    """A muted stretch (digital zeros, 25% of the file) in a meeting with a
    fan: the floor is the fan, not -80 dB, so the pauses in the fan noise are
    still found — and the muted stretch is silence too."""
    src = _make_wav(tmp_path / "muted.wav", 20, loud=[(0, 4), (7, 11)], noise_amp=ROOM_TONE_AMP)
    muted = tmp_path / "muted_tail.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-af", "apad=pad_dur=5", "-c:a", "pcm_s16le",
                    str(muted)], check=True)
    found = detect_silences(muted, SilenceParams(), 25)
    assert found.floor_db > -65
    assert found.ranges == [pytest.approx((4.0, 7.0), abs=0.06), pytest.approx((11.0, 25.0), abs=0.06)]


@needs_ffmpeg
def test_result_is_cached_in_the_job_folder_until_the_source_or_settings_change(tmp_path, monkeypatch):
    src = _make_wav(tmp_path / "room.wav", 8, loud=[(0, 3), (6, 8)], noise_amp=0.004)
    cache = tmp_path / "silences.json"
    first = find_silences(src, cache, SilenceParams(), 8)
    assert cache.exists() and first.ranges == [pytest.approx((3.0, 6.0), abs=0.06)]

    calls = []
    real = silence_module.detect_silences

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(silence_module, "detect_silences", counting)
    again = find_silences(src, cache, SilenceParams(), 8)
    assert calls == [] and again.ranges == first.ranges and again.threshold_db == first.threshold_db
    find_silences(src, cache, SilenceParams(min_s=2.0), 8)  # other parameters: measured again
    assert calls == [1]
    st = src.stat()
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # a replaced source: measured again
    find_silences(src, cache, SilenceParams(min_s=2.0), 8)
    assert calls == [1, 1]


# ------------------------------------------------------------ the rules


def test_adaptive_threshold_rule():
    params = SilenceParams()
    # clean Zoom call: floor -74.4, speech -19.7 -> 35 % of the range
    zoom = np.concatenate([np.full(10, -74.4), np.full(80, -33.0), np.full(10, -19.7)])
    assert adaptive_threshold(zoom, params)[0] == pytest.approx(-74.4 + 0.35 * 54.7, abs=0.01)
    # noisy room with 25 dB of range (f267: typical speech -38, a quiet talker
    # at -45): never within 18 dB of loud speech
    room = np.concatenate([np.full(10, -58.9), np.full(40, -45.0), np.full(40, -38.0), np.full(10, -33.6)])
    assert adaptive_threshold(room, params) == pytest.approx((-33.6 - 18.0, -58.9, -33.6, -38.0), abs=0.01)
    # the same room with music louder than anyone for the top 10%: p95 is the
    # music; typical speech keeps the line 10 dB under it (was -43.2)
    music = np.concatenate([np.full(10, -58.9), np.full(40, -45.0), np.full(40, -38.0), np.full(10, -14.0)])
    assert adaptive_threshold(music, params)[0] == pytest.approx(-38.0 - 10.0, abs=0.01)
    # digital zeros (a muted stretch) are left out of the statistics
    muted = np.concatenate([np.full(30, -120.0), room])
    assert adaptive_threshold(muted, params) == adaptive_threshold(room, params)
    # a noise gate with no room tone left: the zeros are the pauses, floor -80
    gated = np.concatenate([np.full(30, -120.0), np.full(60, -33.0), np.full(10, -19.7)])
    assert adaptive_threshold(gated, params)[:2] == pytest.approx((-80.0 + 0.35 * 60.3, -80.0), abs=0.01)


@pytest.mark.parametrize("floor,speech", [(-60.0, -50.0), (-42.6, -32.5), (-53.0, -33.0)])
def test_no_level_both_6_db_over_room_tone_and_18_db_under_speech_cuts_nothing(monkeypatch, floor, speech):
    """10, 10.1 and 20 dB between room tone and loud speech: the old rule let
    the 6 dB minimum override the 18 dB headroom and put the line 4 dB under
    speech (a noisy room lost 2.8 s of its quiet talker)."""
    levels = np.concatenate([np.full(10, floor), np.full(80, (floor + speech) / 2), np.full(10, speech)])
    threshold, found_floor, *_ = adaptive_threshold(levels, SilenceParams())
    assert threshold - found_floor < 6
    monkeypatch.setattr(silence_module, "window_levels", lambda *a: levels)
    found = detect_silences(None, SilenceParams(), 5)
    assert found.ranges == [] and "nothing is cut" in found.note


def test_no_usable_dynamic_range_cuts_nothing(monkeypatch):
    for levels in (np.full(400, -40.0), np.full(400, -120.0)):  # constant hum; all digital silence
        monkeypatch.setattr(silence_module, "window_levels", lambda *a, lv=levels: lv)
        found = detect_silences(None, SilenceParams(), 20)
        assert found.ranges == [] and "nothing is cut" in found.note


def test_silent_runs_need_the_minimum_length_and_are_never_bridged():
    levels = np.array([-20.0] * 4 + [-80.0] * 19 + [-20.0] + [-80.0] * 20 + [-20.0] * 2)
    assert silent_runs(levels, -50.0, 1.0) == [(1.2, 2.2)]  # 19 windows (0.95 s) are too short
    assert silent_runs(levels, -50.0, 0.9) == [(0.2, 1.15), (1.2, 2.2)]


def test_a_silence_reaching_the_end_of_the_audio_runs_to_the_end_of_the_video():
    assert reach_end([(1.0, 2.0), (50.0, 59.95)], audio_s=59.95, end_s=60.2) == [(1.0, 2.0), (50.0, 60.2)]
    assert reach_end([(1.0, 2.0)], audio_s=59.95, end_s=60.2) == [(1.0, 2.0)]
    assert reach_end([], audio_s=0.0, end_s=60.2) == []


def test_a_short_window_loses_only_the_silence_at_its_edges():
    silences = [(98.0, 101.0), (110.0, 112.0), (128.0, 135.0)]
    assert trim_to_speech(100.0, 130.0, silences, 0.3, 0.25) == (100.75, 128.3)  # the inside pause stays
    assert trim_to_speech(102.0, 108.0, silences, 0.3, 0.25) == (102.0, 108.0)
    assert trim_to_speech(128.5, 134.0, silences, 0.3, 0.25) == (128.5, 134.0)  # all silence: left alone


def test_silence_map_total():
    assert SilenceMap(ranges=[(1.0, 2.5), (4.0, 5.0)]).total_s == 2.5
