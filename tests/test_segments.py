from core.models import Word
from decide.segments import build_segments


def test_groups_consecutive_same_speaker_words():
    words = [
        Word(word="hello", start=0.0, end=0.4, speaker="A"),
        Word(word="world", start=0.4, end=0.8, speaker="A"),
        Word(word="hi", start=0.8, end=1.0, speaker="B"),
    ]
    segments = build_segments(words)
    assert len(segments) == 2
    assert segments[0].speaker == "A"
    assert segments[0].text == "hello world"
    assert segments[0].start == 0.0
    assert segments[0].end == 0.8
    assert segments[1].speaker == "B"
    assert segments[1].text == "hi"


def test_overlap_candidate_propagates_to_segment():
    words = [
        Word(word="hello", start=0.0, end=0.4, speaker="A", overlap_candidate=False),
        Word(word="world", start=0.4, end=0.8, speaker="A", overlap_candidate=True),
    ]
    segments = build_segments(words)
    assert segments[0].overlap_candidate is True


def test_empty_input():
    assert build_segments([]) == []
