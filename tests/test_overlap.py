from core.models import Word
from transcribe.overlap import flag_overlaps


def test_no_overlap_between_same_speaker():
    words = [
        Word(word="hi", start=0.0, end=0.5, speaker="A"),
        Word(word="there", start=0.5, end=1.0, speaker="A"),
    ]
    flagged = flag_overlaps(words)
    assert not any(w.overlap_candidate for w in flagged)


def test_overlap_flagged_for_different_speakers():
    words = [
        Word(word="so", start=10.0, end=10.5, speaker="A"),
        Word(word="yeah", start=10.3, end=10.6, speaker="B"),  # starts before A ends
        Word(word="okay", start=10.6, end=11.0, speaker="B"),
    ]
    flagged = flag_overlaps(words)
    assert flagged[0].overlap_candidate is True
    assert flagged[1].overlap_candidate is True
    assert flagged[2].overlap_candidate is False


def test_adjacent_different_speaker_no_time_overlap_not_flagged():
    words = [
        Word(word="done", start=1.0, end=1.5, speaker="A"),
        Word(word="okay", start=1.5, end=2.0, speaker="B"),
    ]
    flagged = flag_overlaps(words)
    assert not any(w.overlap_candidate for w in flagged)


def test_sorts_by_start_time():
    words = [
        Word(word="second", start=2.0, end=2.5, speaker="B"),
        Word(word="first", start=0.0, end=0.5, speaker="A"),
    ]
    flagged = flag_overlaps(words)
    assert [w.word for w in flagged] == ["first", "second"]
