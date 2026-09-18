from transcribe.native import (
    _parse_json3,
    _pick_track,
    load_uploaded_transcript,
    parse_subtitle_text,
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
    assert len(segments) == 2


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
