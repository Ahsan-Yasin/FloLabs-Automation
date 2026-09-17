from core.models import EditDecisionList, EDLRange, Word
from slice.transcript import remap_transcript


def test_remaps_words_onto_trimmed_timeline():
    words = [
        Word(word="a", start=0.0, end=1.0, speaker="A"),
        Word(word="b", start=1.0, end=2.0, speaker="A"),  # cut
        Word(word="c", start=2.0, end=3.0, speaker="A"),  # cut
        Word(word="d", start=3.0, end=4.0, speaker="B"),
    ]
    # keep [0,1) and [3,4) -> output timeline is [0,1) then [1,2)
    edl = EditDecisionList(ranges=[EDLRange(start=0.0, end=1.0), EDLRange(start=3.0, end=4.0)], source_duration=4.0)

    clean = remap_transcript(words, edl)

    assert [w.word for w in clean] == ["a", "d"]
    assert clean[0].start == 0.0 and clean[0].end == 1.0
    assert clean[1].start == 1.0 and clean[1].end == 2.0


def test_drops_words_that_were_cut():
    words = [Word(word="gone", start=1.0, end=2.0, speaker="A")]
    edl = EditDecisionList(ranges=[EDLRange(start=0.0, end=0.5)], source_duration=2.0)
    assert remap_transcript(words, edl) == []
