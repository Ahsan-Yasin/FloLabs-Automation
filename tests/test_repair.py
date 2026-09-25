from core.models import SegmentJudgment
from decide.repair import repair_fragments


def _j(i, text, decision="keep", speaker="A", start=None, length=None):
    start = i * 5.0 if start is None else start
    length = length if length is not None else min(4.5, 0.4 * len(text.split()) + 0.5)
    return SegmentJudgment(index=i, start=start, end=start + length, speaker=speaker, text=text, decision=decision,
                           removal_category="filler" if decision == "remove" else "none")


def test_removed_tail_of_a_kept_sentence_is_put_back():
    js = [_j(0, "we are basically done with for the", start=0.0, length=4.8),
          _j(1, "legal team.", "remove", start=5.0)]
    out, n = repair_fragments(js)
    assert n == 1 and out[1].decision == "keep" and out[1].removal_category == "none"


def test_removed_head_of_a_kept_sentence_is_put_back():
    js = [_j(0, "Right, so the", "remove", start=0.0, length=1.2), _j(1, "cache cut latency by half.", start=1.5)]
    out, n = repair_fragments(js)
    assert n == 1 and out[0].decision == "keep"


def test_complete_sentences_and_other_speakers_are_left_alone():
    js = [_j(0, "That is the plan.", start=0.0), _j(1, "okay.", "remove", start=4.0)]
    assert repair_fragments(js)[1] == 0  # previous sentence was finished
    js = [_j(0, "we are basically done with the", start=0.0, length=4.8),
          _j(1, "yeah yeah", "remove", speaker="B", start=5.0)]
    assert repair_fragments(js)[1] == 0  # different speaker
    js = [_j(0, "we are basically done with the", start=0.0, length=4.8),
          _j(1, "and then a long removed stretch of small talk about the weekend and the weather", "remove",
             start=5.0, length=12.0)]
    assert repair_fragments(js)[1] == 0  # not a fragment
    js = [_j(0, "we are basically done with the", start=0.0, length=2.0),
          _j(1, "team.", "remove", start=6.0)]
    assert repair_fragments(js)[1] == 0  # a long pause: not the same sentence
