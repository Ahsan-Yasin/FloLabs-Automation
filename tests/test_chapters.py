import pytest

from core.config import get_settings
from core.models import Chapter, Word
from decide.chapters import (
    chapter_count_range,
    chapter_lines,
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

    def generate(self, system, contents, schema=None):
        self.calls.append(contents)
        return _Resp(self.answers.pop(0))


def _answer(*pairs):
    return "\n".join(f"{i}|{t}" for i, t in pairs)


@pytest.fixture(autouse=True)
def _settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_lines_are_numbered_sentences_with_cleaned_time():
    assert chapter_lines(_words(3, step=65.0)).splitlines() == [
        "[0 00:00] sentence 0.", "[1 01:05] sentence 1.", "[2 02:10] sentence 2."]


def test_count_range_scales_with_length():
    assert chapter_count_range(300) == (3, 3)
    lo, hi = chapter_count_range(5400)
    assert 3 <= lo <= hi <= 15 and hi >= 10


def test_titles_are_cleaned_without_eating_real_words():
    assert clean_title("12:30 - <Intro> & overview\n") == "Intro & overview"
    assert clean_title("1. Roadmap") == "Roadmap"
    assert clean_title("3D viewer demo") == "3D viewer demo"
    assert clean_title("2026 goals") == "2026 goals"
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


def test_with_a_reel_the_first_topic_starts_where_the_meeting_starts():
    """Regression: a first topic at 48 s (cleaned) was placed at 230 s, so
    "Highlights" also covered the meeting's first 48 s."""
    chapters = [Chapter(start=48.0, title="Roadmap"), Chapter(start=300.0, title="Demo"),
                Chapter(start=500.0, title="Q&A")]
    entries, problems = finalize_chapters(chapters, final_duration_s=900, reel_s=182.4)
    assert problems == [] and entries[:2] == [(0, "Highlights"), (182, "Roadmap")]


def test_the_intro_is_part_of_highlights_and_shifts_the_topics():
    chapters = [Chapter(start=0.0, title="Welcome"), Chapter(start=120.0, title="Roadmap"),
                Chapter(start=400.0, title="Q&A")]
    entries, problems = finalize_chapters(chapters, final_duration_s=910, reel_s=182.4, intro_s=6.92)
    assert problems == []
    assert entries == [(0, "Highlights"), (189, "Welcome"), (309, "Roadmap"), (589, "Q&A")]


def test_without_a_reel_the_first_topic_covers_the_intro():
    chapters = [Chapter(start=5.0, title="Welcome"), Chapter(start=120.0, title="Roadmap"),
                Chapter(start=400.0, title="Q&A")]
    entries, problems = finalize_chapters(chapters, final_duration_s=610, intro_s=6.92)
    assert problems == [] and entries == [(0, "Welcome"), (126, "Roadmap"), (406, "Q&A")]


def test_the_outro_only_lengthens_the_last_chapter():
    chapters = [Chapter(start=0.0, title="A"), Chapter(start=100.0, title="B"), Chapter(start=195.0, title="C")]
    # 200 s meeting: C (5 s) is too short and dropped, leaving too few chapters
    assert finalize_chapters(chapters, final_duration_s=200)[0] == []
    # + a 5 s outro: C now runs 10 s and is kept
    entries, problems = finalize_chapters(chapters, final_duration_s=205.0)
    assert problems == [] and entries[-1] == (195, "C")


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


def test_chapters_start_at_the_introducing_sentence():
    caller = _Caller([_answer((0, "Intro"), (25, "Roadmap"), (48, "Hiring"), (80, "Wrap-up"))])
    result = generate_chapters(_words(), 1200, caller=caller)
    assert result.ok
    assert [(c.start, c.title) for c in result.chapters] == [
        (0.0, "Intro"), (250.0, "Roadmap"), (480.0, "Hiring"), (800.0, "Wrap-up")]


def test_generate_retries_once_with_the_problems_then_succeeds():
    caller = _Caller([_answer((0, "Only one")), _answer((0, "Intro"), (25, "Roadmap"), (48, "Hiring"),
                                                        (80, "Wrap-up"))])
    result = generate_chapters(_words(), 1200, caller=caller)
    assert result.ok and len(result.chapters) == 4
    assert "rejected" in caller.calls[1]


def test_a_chapter_much_longer_than_the_rest_gets_one_retry():
    words = _words(400)  # 4000 s
    lopsided = _answer((0, "A"), (20, "B"), (40, "C"), (60, "Everything else"))  # last one is 3400 s
    better = _answer((0, "A"), (20, "B"), (40, "C"), (60, "D"), (150, "E"), (250, "F"), (330, "G"))
    caller = _Caller([lopsided, better])
    result = generate_chapters(words, 4000, caller=caller)
    assert result.ok and len(result.chapters) == 7
    assert "split it" in caller.calls[1]
    # still lopsided after the retry: accepted (quality issue, not invalid)
    caller = _Caller([lopsided, lopsided])
    assert generate_chapters(words, 4000, caller=caller).ok


def test_generate_gives_up_after_two_bad_answers():
    caller = _Caller(["not chapters at all", ""])
    result = generate_chapters(_words(), 1200, caller=caller)
    assert not result.ok and result.chapters == [] and result.problems


def test_generate_skips_short_videos_without_calling():
    caller = _Caller([])
    result = generate_chapters(_words(2), 20, caller=caller)
    assert not result.ok and caller.calls == []


def test_invalid_lines_and_duplicates_are_ignored():
    answer = _answer((0, "Intro"), (0, "dup"), (999, "nope"), (40, "Roadmap"), (90, "   "), (100, "Q&A"))
    result = generate_chapters(_words(), 1200, caller=_Caller([answer]))
    assert [c.title for c in result.chapters] == ["Intro", "Roadmap", "Q&A"]


def test_a_suspiciously_short_chapter_gets_one_retry():
    words = _words(200)  # 2000 s
    misnumbered = _answer((0, "Intro"), (3, "Roadmap"), (80, "Hiring"), (140, "Wrap-up"))  # 30 s chapter
    fixed = _answer((0, "Intro"), (40, "Roadmap"), (80, "Hiring"), (140, "Wrap-up"))
    caller = _Caller([misnumbered, fixed])
    result = generate_chapters(words, 2000, caller=caller)
    assert result.ok and [c.start for c in result.chapters] == [0.0, 400.0, 800.0, 1400.0]
    assert "lasts only" in caller.calls[1]
