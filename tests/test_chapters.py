import json

import pytest

from core.config import get_settings
from core.models import Chapter, Word
from decide import chapters as chapters_module
from decide.chapters import (
    build_blocks,
    chapter_count_range,
    clean_title,
    finalize_chapters,
    format_chapters,
    generate_chapters,
    validate_chapter_entries,
)


def _words(n=120, step=10.0):
    return [Word(word=f"sentence {i}.", start=i * step, end=i * step + 8, speaker="A") for i in range(n)]


class _Resp:
    def __init__(self, text):
        self.text = text


class _Caller:
    model = "m"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def generate(self, system, contents, schema):
        self.calls.append(contents)
        return _Resp(self.answers.pop(0))


@pytest.fixture(autouse=True)
def _settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_blocks_start_at_sentence_starts():
    blocks = build_blocks(_words(12), block_s=50)
    assert [b["start"] for b in blocks] == [0.0, 50.0, 100.0]
    assert blocks[0]["text"].startswith("sentence 0.")


def test_count_range_scales_with_length():
    assert chapter_count_range(300) == (3, 3)
    lo, hi = chapter_count_range(5400)
    assert 3 <= lo <= hi <= 15 and hi >= 10


def test_titles_are_cleaned():
    assert clean_title("12:30 - <Intro> & overview\n") == "Intro & overview"
    assert len(clean_title("x" * 100)) == 60


def test_no_reel_forces_first_chapter_to_zero():
    chapters = [Chapter(start=5.0, title="Welcome"), Chapter(start=120.0, title="Roadmap"),
                Chapter(start=400.0, title="Q&A")]
    entries, problems = finalize_chapters(chapters, final_duration_s=600)
    assert problems == []
    assert entries == [(0, "Welcome"), (120, "Roadmap"), (400, "Q&A")]


def test_reel_prepends_highlights_and_offsets_by_its_length():
    chapters = [Chapter(start=0.0, title="Welcome"), Chapter(start=120.0, title="Roadmap"),
                Chapter(start=400.0, title="Q&A")]
    entries, problems = finalize_chapters(chapters, final_duration_s=900, reel_s=182.4)
    assert problems == []
    assert entries == [(0, "Highlights"), (182, "Welcome"), (302, "Roadmap"), (582, "Q&A")]


def test_too_close_and_too_late_chapters_are_dropped():
    chapters = [Chapter(start=0.0, title="A"), Chapter(start=4.0, title="too close"),
                Chapter(start=100.0, title="B"), Chapter(start=200.0, title="C"), Chapter(start=295.0, title="late")]
    entries, problems = finalize_chapters(chapters, final_duration_s=300)
    assert problems == [] and [t for t, _ in entries] == [0, 100, 200]


def test_fewer_than_three_is_rejected_not_published():
    entries, problems = finalize_chapters([Chapter(start=0, title="A"), Chapter(start=60, title="B")],
                                          final_duration_s=600)
    assert entries == [] and "at least 3" in problems[0]


def test_format_uses_hours_past_an_hour():
    text = format_chapters([(0, "Highlights"), (185, "Intro"), (3725, "Wrap-up")], 4000)
    assert text.splitlines() == ["00:00 Highlights", "03:05 Intro", "1:02:05 Wrap-up"]


def test_validation_rules():
    assert validate_chapter_entries([(0, "a"), (10, "b"), (20, "c")], 40) == []
    assert validate_chapter_entries([(1, "a"), (11, "b"), (21, "c")], 40)  # not at 00:00
    assert validate_chapter_entries([(0, "a"), (5, "b"), (20, "c")], 40)  # < 10 s apart
    assert validate_chapter_entries([(0, "a"), (10, "b<"), (20, "c")], 40)  # bad title
    long = [(i * 10, "x" * 60) for i in range(90)]
    assert any("5000" in p for p in validate_chapter_entries(long, 2000))


def test_generate_retries_once_with_the_problems_then_succeeds():
    bad = json.dumps({"chapters": [{"block": 0, "title": "Only one"}]})
    good = json.dumps({"chapters": [{"block": 0, "title": "Intro"}, {"block": 5, "title": "Roadmap"},
                                    {"block": 12, "title": "Hiring"}]})
    caller = _Caller([bad, good])
    result = generate_chapters(_words(), 1200, caller=caller)
    assert result.ok
    assert [c.title for c in result.chapters] == ["Intro", "Roadmap", "Hiring"]
    assert result.chapters[1].start == 250.0  # block 5 starts at a sentence start
    assert "rejected" in caller.calls[1]


def test_generate_gives_up_after_two_bad_answers():
    caller = _Caller(["not json", json.dumps({"chapters": []})])
    result = generate_chapters(_words(), 1200, caller=caller)
    assert not result.ok and result.chapters == [] and result.problems


def test_generate_skips_short_videos_without_calling():
    caller = _Caller([])
    result = generate_chapters(_words(2), 20, caller=caller)
    assert not result.ok and caller.calls == []


def test_invalid_blocks_and_duplicates_are_ignored():
    answer = json.dumps({"chapters": [{"block": 0, "title": "Intro"}, {"block": 0, "title": "dup"},
                                      {"block": 999, "title": "nope"}, {"block": 4, "title": "Roadmap"},
                                      {"block": 9, "title": "   "}, {"block": 10, "title": "Q&A"}]})
    result = generate_chapters(_words(), 1200, caller=_Caller([answer]))
    assert [c.title for c in result.chapters] == ["Intro", "Roadmap", "Q&A"]
    assert chapters_module.MIN_CHAPTERS == 3
