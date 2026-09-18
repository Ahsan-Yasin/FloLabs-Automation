from itertools import pairwise

from core.models import Word
from transcribe.native import (
    _group_into_sentences,
    _parse_json3,
    _pick_track,
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
