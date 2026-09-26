import json
import sys
from itertools import pairwise
from pathlib import Path
from typing import ClassVar

from core.models import Word
from transcribe import native
from transcribe.native import (
    DEFAULT_SPEAKER,
    _group_into_sentences,
    _parse_json3,
    _parse_youtube_vtt,
    _pick_track,
    fetch_youtube_transcript,
    load_uploaded_transcript,
    parse_subtitle_text,
    segments_to_words,
)


def test_parse_vtt_plain_cues_have_no_speaker_prefix():
    vtt = """WEBVTT

00:00:00.000 --> 00:00:02.500
Hello there, welcome back.

00:00:02.500 --> 00:00:05.000
Thanks for tuning in today.
"""
    words, segments = parse_subtitle_text(vtt)
    assert [w.word for w in words] == ["Hello there, welcome back.", "Thanks for tuning in today."]
    assert words[0].start == 0.0
    assert words[0].end == 2.5
    assert words[0].speaker == "SPEAKER"
    assert len(segments) == 2
    assert segments[1].start == 2.5


def test_parse_vtt_voice_tag_sets_speaker():
    vtt = """WEBVTT

00:00:00.000 --> 00:00:03.000
<v Jane>Thanks for having me.</v>
"""
    words, _ = parse_subtitle_text(vtt)
    assert words[0].speaker == "Jane"
    assert words[0].word == "Thanks for having me."


def test_parse_vtt_zoom_style_name_prefix_sets_speaker():
    vtt = """WEBVTT

00:00:00.000 --> 00:00:03.000
John Doe: Let's get started today.

00:00:03.000 --> 00:00:06.000
Jane Smith: Sounds good to me.
"""
    words, segments = parse_subtitle_text(vtt)
    assert words[0].speaker == "John Doe"
    assert words[0].word == "Let's get started today."
    assert words[1].speaker == "Jane Smith"
    assert segments[0].speaker == "John Doe"


def test_parse_srt_comma_decimal_timestamps():
    srt = """1
00:00:00,000 --> 00:00:02,500
Hello there.

2
00:00:02,500 --> 00:00:05,000
Thanks for having me.
"""
    words, _ = parse_subtitle_text(srt)
    assert len(words) == 2
    assert words[0].start == 0.0
    assert words[0].end == 2.5
    assert words[1].word == "Thanks for having me."


def test_parse_subtitle_text_ignores_garbage():
    words, segments = parse_subtitle_text("not a subtitle file at all")
    assert words == []
    assert segments == []


def test_parse_json3_skips_events_without_segs():
    data = {
        "events": [
            {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "Hello there\n"}]},
            {"wsWinStyles": []},
            {"tStartMs": 2000, "dDurationMs": 3000, "segs": [{"utf8": "welcome back"}]},
        ]
    }
    words, segments = _parse_json3(data)
    assert [w.word for w in words] == ["Hello there", "welcome back"]
    assert words[0].end == 2.0
    assert words[1].start == 2.0
    assert words[1].end == 5.0
    # neither cue ends in sentence-terminal punctuation, so they're one sentence
    assert len(segments) == 1
    assert segments[0].text == "Hello there welcome back"
    assert segments[0].start == 0.0
    assert segments[0].end == 5.0


def test_group_into_sentences_merges_a_sentence_split_across_cues():
    """Regression test for the real-world problem: caption cues are timed for
    on-screen readability, not sentence boundaries, so a single sentence
    routinely gets split across two or three consecutive cues — e.g. this
    exact real example from a YouTube auto-caption track."""
    cues = [
        Word(word="I proceed down, let's check if we have", start=8.6, end=14.0, speaker="SPEAKER"),
        Word(word="our new members here haven't introduced", start=10.7, end=17.7, speaker="SPEAKER"),
        Word(word="themselves to the team.", start=14.0, end=17.7, speaker="SPEAKER"),
    ]
    segments = _group_into_sentences(cues)
    assert len(segments) == 1
    assert segments[0].text == (
        "I proceed down, let's check if we have our new members here haven't "
        "introduced themselves to the team."
    )
    assert segments[0].start == 8.6
    assert segments[0].end == 17.7


def test_group_into_sentences_splits_multiple_sentences_in_one_cue():
    """Regression test for a real bug this exact case caused: if all three
    sentences inherited the whole cue's span (identical start/end), a "keep"
    on one and "remove" on another would collide on the same timestamp range
    and the EDL builder would silently let the "keep" win — see
    test_same_cue_split_does_not_let_a_keep_erase_a_neighboring_remove for
    the full end-to-end consequence. Each sentence must get its own
    proportional, non-overlapping slice of the cue's timing instead.
    """
    cues = [Word(word="Yes. Yes, I did. I introduced myself.", start=46.7, end=51.2, speaker="SPEAKER")]
    segments = _group_into_sentences(cues)
    assert [s.text for s in segments] == ["Yes.", "Yes, I did.", "I introduced myself."]

    # 7 tokens total (1 + 3 + 3), so each sentence gets a proportional share
    # of the cue's 4.5s span, in order, with no overlap
    assert segments[0].start == 46.7
    for a, b in pairwise(segments):
        assert a.end == b.start  # contiguous, not overlapping
        assert a.start < a.end  # non-degenerate
    assert segments[-1].end == 51.2


def test_same_cue_split_does_not_let_a_keep_erase_a_neighboring_remove():
    """End-to-end regression test for the actual bug found by tracing a real
    job: interpolated timestamps must be distinct enough that build_edl can
    correctly cut a "remove" sentence even when a "keep" sentence came from
    the very same cue right next to it, instead of the keep's range silently
    overriding the remove's for their (previously identical) shared span.
    """
    from core.models import Decision
    from edl.builder import build_edl

    cues = [Word(word="Okay. Yes, thank you so much. Let's move on.", start=100.0, end=104.5, speaker="SPEAKER")]
    segments = _group_into_sentences(cues)
    assert [s.text for s in segments] == ["Okay.", "Yes, thank you so much.", "Let's move on."]

    decisions = [
        Decision(start=segments[0].start, end=segments[0].end, decision="remove"),
        Decision(start=segments[1].start, end=segments[1].end, decision="remove"),
        Decision(start=segments[2].start, end=segments[2].end, decision="keep"),
    ]
    # matches production (pipeline.py): snap against the sentence-level words
    # derived from segments, NOT the original coarse per-cue list — snapping
    # against the raw cues would expand the interpolated cut back out to the
    # whole cue's span and silently undo the fix.
    words = segments_to_words(segments)
    edl = build_edl(decisions, words, source_duration=200.0)

    # only "Let's move on." should survive — not the whole 100.0-104.5 cue
    assert len(edl.ranges) == 1
    assert edl.ranges[0].start == segments[2].start
    assert edl.ranges[0].start > segments[0].end


def test_group_into_sentences_breaks_on_speaker_change_even_mid_sentence():
    cues = [
        Word(word="So the plan is to go", start=0.0, end=2.0, speaker="A"),
        Word(word="ahead and ship it tomorrow.", start=2.0, end=4.0, speaker="B"),
    ]
    segments = _group_into_sentences(cues)
    assert len(segments) == 2
    assert segments[0].speaker == "A"
    assert segments[0].text == "So the plan is to go"
    assert segments[1].speaker == "B"
    assert segments[1].text == "ahead and ship it tomorrow."


def test_group_into_sentences_caps_runaway_segment_with_no_punctuation():
    cues = [
        Word(word=f"word{i} word{i}", start=float(i * 2), end=float(i * 2 + 2), speaker="SPEAKER")
        for i in range(30)
    ]
    segments = _group_into_sentences(cues)
    # with 60 words and no punctuation at all, the word cap must still force
    # multiple segments rather than growing one unbounded blob
    assert len(segments) > 1
    assert all(len(s.text.split()) <= 40 for s in segments)


def test_pick_track_prefers_manual_over_automatic():
    manual = {"en": [{"ext": "vtt", "url": "manual_vtt"}]}
    automatic = {"en": [{"ext": "json3", "url": "auto_json3"}]}
    track = _pick_track(manual, automatic)
    assert track["url"] == "manual_vtt"


def test_pick_track_prefers_json3_within_same_source():
    automatic = {"en": [{"ext": "vtt", "url": "auto_vtt"}, {"ext": "json3", "url": "auto_json3"}]}
    track = _pick_track({}, automatic)
    assert track["url"] == "auto_json3"


def test_pick_track_returns_none_when_no_english_track():
    track = _pick_track({}, {"fr": [{"ext": "vtt", "url": "fr_vtt"}]})
    assert track is None


def test_load_uploaded_transcript_missing_file_returns_none(tmp_path):
    result = load_uploaded_transcript(tmp_path / "does-not-exist.vtt")
    assert result is None


def test_load_uploaded_transcript_parses_real_file(tmp_path):
    path = tmp_path / "transcript.vtt"
    path.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello world.\n",
        encoding="utf-8",
    )
    result = load_uploaded_transcript(path)
    assert result is not None
    words, segments = result
    assert words[0].word == "Hello world."
    assert len(segments) == 1


# ------------------------------------------------------------------ YouTube captions

# The shape of a real YouTube auto-caption VTT (text made up): each cue opens
# with a single-space line or the previous line again, new words carry karaoke
# timestamp tags, and a 10ms cue in between re-shows the finished line.
_ROLLING_VTT_LINES = (
    "WEBVTT",
    "Kind: captions",
    "Language: en",
    "",
    "00:00:05.220 --> 00:00:08.930 align:start position:0%",
    " ",
    "so<00:00:05.940><c> welcome</c><00:00:06.180><c> to</c><00:00:06.540><c> the</c><00:00:06.900><c> weekly</c>",
    "",
    "00:00:08.930 --> 00:00:08.940 align:start position:0%",
    "so welcome to the weekly",
    " ",
    "",
    "00:00:08.940 --> 00:00:11.150 align:start position:0%",
    "so welcome to the weekly",
    "sync<00:00:09.179><c> meeting</c><00:00:10.019><c> &gt;&gt;</c><00:00:10.320><c> thanks</c>",
    "",
    "00:00:11.150 --> 00:00:11.160 align:start position:0%",
    "sync meeting &gt;&gt; thanks",
    " ",
    "",
    "00:00:11.160 --> 00:00:13.000 align:start position:0%",
    "sync meeting &gt;&gt; thanks",
    "Note:<00:00:11.500><c> slides</c><00:00:12.000><c> are</c><00:00:12.500><c> shared</c>",
    "",
)
_ROLLING_VTT = "\n".join(_ROLLING_VTT_LINES)


def test_parse_youtube_vtt_drops_rolling_repeats():
    words, segments = _parse_youtube_vtt(_ROLLING_VTT)
    assert [(w.start, w.end, w.word) for w in words] == [
        (5.22, 8.93, "so welcome to the weekly"),
        (8.94, 11.15, "sync meeting >> thanks"),
        (11.16, 13.0, "Note: slides are shared"),
    ]
    # each line once; the naive Zoom-path parse sees them two or three times
    text = " ".join(s.text for s in segments)
    assert text == "so welcome to the weekly sync meeting >> thanks Note: slides are shared"
    naive, _ = parse_subtitle_text(_ROLLING_VTT)
    assert len(" ".join(w.word for w in naive).split()) > len(text.split())


def test_parse_youtube_vtt_never_invents_speakers():
    """Captions have no speakers; a caption line shaped like Zoom's "Name: text"
    stays text, as it does on the json3 path."""
    vtt = (
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nJOHN SMITH: hello there.\n\n"
        "00:00:02.000 --> 00:00:04.000\nNote: bring snacks.\n"
    )
    words, segments = _parse_youtube_vtt(vtt)
    assert [(w.speaker, w.word) for w in words] == [
        (DEFAULT_SPEAKER, "JOHN SMITH: hello there."),
        (DEFAULT_SPEAKER, "Note: bring snacks."),
    ]
    assert {s.speaker for s in segments} == {DEFAULT_SPEAKER}


def test_parse_youtube_vtt_leaves_creator_captions_unchanged():
    # Two consecutive "No." cues are speech said twice, not a rolling repeat.
    vtt = (
        "WEBVTT\n\n00:00:01.200 --> 00:00:03.360\nAll right, so here we are,\nin front of the stage\n\n"
        "00:00:04.000 --> 00:00:05.000\nNo.\n\n"
        "00:00:05.500 --> 00:00:06.500\nNo.\n\n"
        "00:00:07.000 --> 00:00:08.000\nand that's all.\n"
    )
    words, _ = _parse_youtube_vtt(vtt)
    assert [(w.start, w.end, w.word) for w in words] == [
        (1.2, 3.36, "All right, so here we are, in front of the stage"),
        (4.0, 5.0, "No."),
        (5.5, 6.5, "No."),
        (7.0, 8.0, "and that's all."),
    ]


def test_parse_youtube_vtt_keeps_a_line_said_twice_in_auto_captions():
    """A rolling cue repeats the old line first; a second line equal to it is
    new speech that happens to match, not the repeat."""
    vtt = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:02.000\n \nno<00:00:01.500><c> way</c>\n\n"
        "00:00:02.000 --> 00:00:02.010\nno way\n \n\n"
        "00:00:02.010 --> 00:00:03.000\nno way\nno<00:00:02.500><c> way</c>\n\n"
        "00:00:03.000 --> 00:00:03.010\nno way\n \n\n"
        "00:00:03.010 --> 00:00:04.000\nno way\nokay<00:00:03.500><c> fine</c>\n"
    )
    words, _ = _parse_youtube_vtt(vtt)
    assert [(w.start, w.word) for w in words] == [(1.0, "no way"), (2.01, "no way"), (3.01, "okay fine")]


def test_parse_json3_clips_auto_caption_events_to_the_next_line():
    """Auto-caption events last while the line is on screen, overlapping the
    next line's speech; left as-is a kept sentence would swallow the start of
    a removed one in the EDL."""
    data = {
        "events": [
            {"tStartMs": 5220, "dDurationMs": 5940, "segs": [{"utf8": "so welcome to the"}]},
            {"tStartMs": 8930, "dDurationMs": 2230, "aAppend": 1, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 8940, "dDurationMs": 6359, "segs": [{"utf8": "weekly sync."}]},
            {"tStartMs": 11160, "dDurationMs": 6300, "segs": [{"utf8": "Let's start."}]},
        ]
    }
    words, segments = _parse_json3(data)
    assert [(w.start, w.end) for w in words] == [(5.22, 8.94), (8.94, 11.16), (11.16, 17.46)]
    assert [s.text for s in segments] == ["so welcome to the weekly sync.", "Let's start."]
    assert segments[0].end <= segments[1].start


def test_pick_track_skips_hls_entries():
    automatic = {
        "en": [
            {"ext": "vtt", "protocol": "m3u8_native", "url": "https://example.test/playlist.m3u8"},
            {"ext": "vtt", "url": "https://example.test/api/timedtext?lang=en&fmt=vtt"},
        ]
    }
    track = _pick_track({}, automatic)
    assert track["url"].endswith("fmt=vtt")


def test_pick_track_skips_machine_translations():
    """Real case: an English meeting where YouTube also ran Arabic ASR, and
    yt-dlp listed "Arabic translated to English" first under "en"."""
    translated = "https://example.test/api/timedtext?caps=asr&kind=asr&lang=ar&tlang=en&fmt=json3"
    original = "https://example.test/api/timedtext?caps=asr&kind=asr&lang=en&fmt=json3"
    automatic = {
        "ar-orig": [{"ext": "json3", "url": "https://example.test/api/timedtext?lang=ar&fmt=json3"}],
        "en": [{"ext": "json3", "url": translated}, {"ext": "json3", "url": original}],
    }
    assert _pick_track({}, automatic)["url"] == original
    assert _pick_track({}, {"en": [{"ext": "json3", "url": translated}]}) is None


# A stand-in yt_dlp: extract_info returns INFO, dl writes the track's payload.
_ORIGINAL_JSON3 = "https://example.test/api/timedtext?lang=en&fmt=json3"
_VTT_ONLY = "https://example.test/api/timedtext?lang=en&fmt=vtt"


class _FakeYDL:
    info: ClassVar[dict] = {}
    payloads: ClassVar[dict] = {}
    failures_left = 0
    instances: ClassVar[list] = []

    def __init__(self, opts):
        self.opts = opts
        _FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        assert download is False
        if _FakeYDL.failures_left:
            _FakeYDL.failures_left -= 1
            raise OSError("read timed out")
        return _FakeYDL.info

    def dl(self, name, info, subtitle=False):
        assert subtitle
        Path(name).write_bytes(_FakeYDL.payloads[info["url"]])
        return True


def _install_fake_ydl(monkeypatch, automatic=None, manual=None, payloads=None, failures=0):
    _FakeYDL.info = {"subtitles": manual or {}, "automatic_captions": automatic or {}}
    _FakeYDL.payloads = payloads or {}
    _FakeYDL.failures_left = failures
    _FakeYDL.instances = []
    monkeypatch.setitem(sys.modules, "yt_dlp", type("m", (), {"YoutubeDL": _FakeYDL}))
    monkeypatch.setattr(native.time, "sleep", lambda s: None)


def test_fetch_youtube_transcript_downloads_the_picked_json3_track(monkeypatch):
    payload = {"events": [{"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "Hello everyone."}]}]}
    _install_fake_ydl(
        monkeypatch,
        automatic={"en": [{"ext": "json3", "url": _ORIGINAL_JSON3}]},
        payloads={_ORIGINAL_JSON3: json.dumps(payload).encode()},
    )
    words, segments = fetch_youtube_transcript("https://youtube.com/watch?v=abc123")
    assert [w.word for w in words] == ["Hello everyone."]
    assert segments[0].speaker == DEFAULT_SPEAKER
    opts = _FakeYDL.instances[0].opts
    assert opts["skip_download"] and opts["noplaylist"] and "logger" in opts


def test_fetch_youtube_transcript_vtt_fallback_uses_the_rolling_parser(monkeypatch):
    _install_fake_ydl(
        monkeypatch,
        automatic={"en": [{"ext": "vtt", "url": _VTT_ONLY}]},
        payloads={_VTT_ONLY: _ROLLING_VTT.encode()},
    )
    words, _ = fetch_youtube_transcript("https://youtube.com/watch?v=abc123")
    assert [w.word for w in words][:2] == ["so welcome to the weekly", "sync meeting >> thanks"]


def test_fetch_youtube_transcript_without_an_english_track_returns_none(monkeypatch):
    _install_fake_ydl(monkeypatch, automatic={"fr": [{"ext": "json3", "url": _ORIGINAL_JSON3}]})
    assert fetch_youtube_transcript("https://youtube.com/watch?v=abc123") is None


def test_fetch_youtube_transcript_retries_once(monkeypatch):
    payload = {"events": [{"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "Second time lucky."}]}]}
    _install_fake_ydl(
        monkeypatch,
        automatic={"en": [{"ext": "json3", "url": _ORIGINAL_JSON3}]},
        payloads={_ORIGINAL_JSON3: json.dumps(payload).encode()},
        failures=1,
    )
    words, _ = fetch_youtube_transcript("https://youtube.com/watch?v=abc123")
    assert words[0].word == "Second time lucky."
    assert len(_FakeYDL.instances) == 2


def test_fetch_youtube_transcript_never_raises(monkeypatch):
    _install_fake_ydl(monkeypatch, automatic={"en": [{"ext": "json3", "url": _ORIGINAL_JSON3}]}, failures=2)
    assert fetch_youtube_transcript("https://youtube.com/watch?v=abc123") is None
    assert len(_FakeYDL.instances) == 2
