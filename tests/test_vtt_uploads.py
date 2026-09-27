"""Uploaded transcript parsing (.vtt/.srt exports from meeting tools, Zoom included): the real-file quirks parse_subtitle_text
tolerates and the "Name: text" speaker heuristic (see transcribe.native)."""

import pytest

from transcribe.native import (
    DEFAULT_SPEAKER,
    load_uploaded_transcript,
    parse_subtitle_text,
)


def _vtt(*cues: str) -> str:
    return "WEBVTT\n\n" + "\n\n".join(cues) + "\n"


# ------------------------------------------------------------------ timestamps and file shape


def test_timestamps_without_hours_and_with_short_fractions():
    words, _ = parse_subtitle_text(
        _vtt(
            "00:01.5 --> 00:03.25\nOne and a half to three and a quarter.",
            "01:02.000 --> 01:04.500\nPast the first minute.",
            "1:00:00.000 --> 1:00:02.000\nOne hour in.",
        )
    )
    assert [(w.start, w.end) for w in words] == [(1.5, 3.25), (62.0, 64.5), (3600.0, 3602.0)]


def test_cue_settings_after_the_timing_line_are_ignored():
    words, _ = parse_subtitle_text(_vtt("00:00:00.000 --> 00:00:02.000 align:start position:0%\nHello."))
    assert (words[0].start, words[0].end, words[0].word) == (0.0, 2.0, "Hello.")


def test_crlf_line_endings_and_bom():
    text = (
        "﻿WEBVTT\r\n\r\n1\r\n00:00:00.000 --> 00:00:02.000\r\nAlice Jones: Hello there.\r\n\r\n"
        "2\r\n00:00:02.000 --> 00:00:04.000\r\nBob Lee: Hi Alice.\r\n"
    )
    words, _ = parse_subtitle_text(text)
    assert [(w.speaker, w.word) for w in words] == [("Alice Jones", "Hello there."), ("Bob Lee", "Hi Alice.")]


def test_load_uploaded_transcript_reads_a_bom_crlf_file(tmp_path):
    path = tmp_path / "audio_transcript.vtt"
    path.write_bytes("WEBVTT\r\n\r\n00:00:00.000 --> 00:00:02.000\r\nAlice Jones: Hello there.\r\n".encode("utf-8-sig"))
    words, segments = load_uploaded_transcript(path)
    assert (words[0].speaker, words[0].word) == ("Alice Jones", "Hello there.")
    assert segments[0].text == "Hello there."


def test_optional_identifiers_and_a_missing_blank_line_between_cues():
    # Cue 1 is followed directly by cue 2's identifier and timing line, and
    # cue 3 has no identifier or blank line at all: the bare "2" must not
    # become part of cue 1's speech.
    text = (
        "WEBVTT\n\nintro\n00:00:00.000 --> 00:00:02.000\nAlice Jones: First line.\n"
        "2\n00:00:02.000 --> 00:00:04.000\nAlice Jones: Second line.\n"
        "00:00:04.000 --> 00:00:06.000\nAlice Jones: Third line.\n"
    )
    words, _ = parse_subtitle_text(text)
    assert [w.word for w in words] == ["First line.", "Second line.", "Third line."]
    assert [w.start for w in words] == [0.0, 2.0, 4.0]


def test_vtt_escapes_are_decoded():
    words, _ = parse_subtitle_text(_vtt("00:00:00.000 --> 00:00:02.000\nQ&amp;A comes after the &lt;demo&gt;."))
    assert words[0].word == "Q&A comes after the <demo>."


# ------------------------------------------------------------------ "Name: text" speakers


@pytest.mark.parametrize("name", ["Graham Robinson", "Smith, John", "Hareem (FloLabs)", "jdoe@example.com"])
def test_display_name_prefix_is_a_speaker_even_when_it_appears_once(name):
    words, _ = parse_subtitle_text(_vtt(f"00:00:00.000 --> 00:00:02.000\n{name}: Let's get started."))
    assert (words[0].speaker, words[0].word) == (name, "Let's get started.")


def test_an_odd_name_is_a_speaker_once_it_recurs():
    once, _ = parse_subtitle_text(_vtt("00:00:00.000 --> 00:00:02.000\njdoe: Just once."))
    assert (once[0].speaker, once[0].word) == (DEFAULT_SPEAKER, "jdoe: Just once.")

    twice, _ = parse_subtitle_text(
        _vtt("00:00:00.000 --> 00:00:02.000\njdoe: First point.", "00:00:02.000 --> 00:00:04.000\njdoe: Second point.")
    )
    assert [(w.speaker, w.word) for w in twice] == [("jdoe", "First point."), ("jdoe", "Second point.")]


@pytest.mark.parametrize(
    "name",
    ["So Yeon Park", "Christopher Montgomery-Wellington (he/him)", "Graham Robinson | Head of Engineering, FloLabs"],
)
def test_a_recurring_name_wins_over_the_opener_and_length_checks(name):
    # "So" is a stop-listed opener and both long names are past the 40-char
    # one-off shape limit; left as text, the name would reach the LLM and the
    # burned-in captions as speech.
    words, segments = parse_subtitle_text(
        _vtt(
            f"00:00:00.000 --> 00:00:02.000\n{name}: Good morning everyone.",
            "00:00:02.000 --> 00:00:04.000\nAlice Jones: Morning.",
            f"00:00:04.000 --> 00:00:06.000\n{name}: Let's start with the budget.",
        )
    )
    assert [(w.speaker, w.word) for w in words] == [
        (name, "Good morning everyone."),
        ("Alice Jones", "Morning."),
        (name, "Let's start with the budget."),
    ]
    assert segments[0].text == "Good morning everyone."


@pytest.mark.parametrize(
    "line",
    [
        "Note: the deadline moved to Friday.",
        "So the answer is: yes.",
        "Here's the thing: we ship on Friday.",
        "Thanks Everyone: see you next week.",
    ],
)
def test_a_colon_in_speech_is_not_a_speaker(line):
    words, _ = parse_subtitle_text(_vtt(f"00:00:00.000 --> 00:00:02.000\n{line}"))
    assert (words[0].speaker, words[0].word) == (DEFAULT_SPEAKER, line)


def test_a_stop_listed_label_stays_text_even_when_it_recurs():
    words, _ = parse_subtitle_text(
        _vtt("00:00:00.000 --> 00:00:02.000\nNote: first.", "00:00:02.000 --> 00:00:04.000\nNote: second.")
    )
    assert [(w.speaker, w.word) for w in words] == [(DEFAULT_SPEAKER, "Note: first."), (DEFAULT_SPEAKER, "Note: second.")]


def test_unattributed_cue_gets_the_default_speaker():
    words, _ = parse_subtitle_text(_vtt("00:00:00.000 --> 00:00:02.000\njust some words without a name"))
    assert (words[0].speaker, words[0].word) == (DEFAULT_SPEAKER, "just some words without a name")


# ------------------------------------------------------------------ <v> voice tags


def test_voice_tag_with_a_class_sets_the_speaker():
    words, _ = parse_subtitle_text(_vtt("00:00:00.000 --> 00:00:02.000\n<v.loud Alice Jones>We're live.</v>"))
    assert (words[0].speaker, words[0].word) == ("Alice Jones", "We're live.")


def test_two_voices_in_one_cue_get_distinct_contiguous_spans():
    words, segments = parse_subtitle_text(
        _vtt("00:00:00.000 --> 00:00:07.000\n<v Alice>Are we ready?</v> <v Bob>Yes, let's start now.</v>")
    )
    assert [(w.speaker, w.word) for w in words] == [("Alice", "Are we ready?"), ("Bob", "Yes, let's start now.")]
    # split in proportion to word count (3 of 7 words, then 4 of 7)
    assert words[0].start == 0.0
    assert words[0].end == pytest.approx(3.0)
    assert words[0].end == words[1].start
    assert words[1].end == 7.0
    assert [s.speaker for s in segments] == ["Alice", "Bob"]


# ------------------------------------------------------------------ max_duration_s


def test_max_duration_drops_cues_past_the_end_and_clamps_a_straddling_one():
    text = _vtt(
        "00:00:00.000 --> 00:00:05.000\nAlice Jones: Opening remarks.",
        "00:00:05.000 --> 00:00:12.000\nAlice Jones: This runs past the end.",
        "00:00:10.000 --> 00:00:11.000\nAlice Jones: Starts exactly at the end.",
        "00:00:12.000 --> 00:00:15.000\nAlice Jones: Entirely past the end.",
    )
    words, segments = parse_subtitle_text(text, max_duration_s=10.0)
    assert [w.word for w in words] == ["Opening remarks.", "This runs past the end."]
    assert words[-1].end == 10.0
    assert segments[-1].end == 10.0

    unclamped, _ = parse_subtitle_text(text)
    assert len(unclamped) == 4


# ------------------------------------------------------------------ sentence grouping


def test_sentences_group_across_cues_but_break_on_a_speaker_change():
    words, segments = parse_subtitle_text(
        _vtt(
            "00:00:00.000 --> 00:00:02.000\nAlice Jones: So the plan is",
            "00:00:02.000 --> 00:00:04.000\nAlice Jones: to go ahead and",
            "00:00:04.000 --> 00:00:06.000\nBob Lee: ship it tomorrow.",
        )
    )
    assert len(words) == 3
    assert [(s.speaker, s.text) for s in segments] == [
        ("Alice Jones", "So the plan is to go ahead and"),
        ("Bob Lee", "ship it tomorrow."),
    ]
    assert segments[0].end == segments[1].start == 4.0
