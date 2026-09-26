from core.models import EditDecisionList, EDLRange, Word
from slice.transcript import merge_spans, remap_transcript


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


def test_word_straddling_a_frame_snapped_boundary_is_kept_and_clamped():
    from core.models import RenderManifest, RenderPiece

    # keep source frames [30, 150) @30fps = 1.0-5.0 s, output starts at 0
    manifest = RenderManifest(kind="cleaned", fps="30/1", width=2, height=2,
                              pieces=[RenderPiece(src_start_frame=30, src_end_frame=150, out_start_frame=0)])
    words = [
        Word(word="edge", start=0.99, end=1.5, speaker="A"),  # starts 10 ms before the cut
        Word(word="tail", start=4.8, end=5.02, speaker="A"),  # ends 20 ms after
        Word(word="gone", start=5.3, end=5.9, speaker="A"),
    ]
    clean = remap_transcript(words, manifest=manifest)
    assert [w.word for w in clean] == ["edge", "tail"]
    assert clean[0].start == 0.0 and clean[0].end == 0.5
    assert clean[1].end == 4.0


def test_remap_via_manifest_matches_hard_cut_formula():
    from core.models import RenderManifest, RenderPiece

    manifest = RenderManifest(kind="cleaned", fps="25/1", width=2, height=2, pieces=[
        RenderPiece(src_start_frame=0, src_end_frame=100, out_start_frame=0),
        RenderPiece(src_start_frame=250, src_end_frame=400, out_start_frame=100),
    ])
    words = [Word(word="x", start=11.0, end=11.5, speaker="B")]
    (w,) = remap_transcript(words, manifest=manifest)
    assert (w.start, w.end) == (5.0, 5.5)  # 11.0 - 10.0 + 4.0


def test_a_sentence_straddling_a_cut_pause_is_still_said():
    """A caption sentence's timing spans its pauses: when the pause cut in it
    holds the sentence's midpoint, the sentence goes to the kept part it
    overlaps most (not silently into the removed transcript)."""
    from core.models import RemovedRange, RenderManifest, RenderPiece
    from report.text import removed_entries

    # kept 0-4 s and 9-10 s @25 fps; 4-9 s is a cut pause
    manifest = RenderManifest(kind="cleaned", fps="25/1", width=2, height=2, pieces=[
        RenderPiece(src_start_frame=0, src_end_frame=100, out_start_frame=0),
        RenderPiece(src_start_frame=225, src_end_frame=250, out_start_frame=100),
    ])
    pause = RemovedRange(start=4.0, end=9.0, start_frame=100, end_frame=225, tier="transcript_only",
                         reason="silence (no one speaking)", silence=True)
    sentence = Word(word="we shipped it (long pause) yesterday", start=1.0, end=10.0, speaker="A")  # midpoint 5.5
    mistimed = Word(word="okay", start=5.0, end=6.0, speaker="B")  # wholly inside the pause: really not said

    assert remap_transcript([sentence], manifest=manifest) == []  # the old midpoint rule lost it
    clean = remap_transcript([sentence, mistimed], manifest=manifest, silence=[(4.0, 9.0)])
    assert [(w.word, w.start, w.end) for w in clean] == [(sentence.word, 1.0, 4.0)]  # 3 s kept there vs 1 s
    (entry,) = removed_entries([pause], [sentence, mistimed])
    assert [line["text"] for line in entry.lines] == ["okay"] and entry.silence


def test_pause_spans_that_overlap_are_merged_before_the_lookup():
    """deliver passes the pause cuts (seconds) AND the pure-silence ranges
    (frame-rounded): nested spans must not hide the one holding a midpoint."""
    assert merge_spans([(4.02, 9.0), (12.0, 13.0), (4.0, 9.02)]) == [(4.0, 9.02), (12.0, 13.0)]
    edl = EditDecisionList(ranges=[EDLRange(start=0.0, end=4.0), EDLRange(start=9.04, end=12.0)],
                           source_duration=12.0)
    sentence = Word(word="said before the pause", start=0.0, end=18.02, speaker="A")  # midpoint 9.01
    assert [w.word for w in remap_transcript([sentence], edl, silence=[(4.0, 9.02), (4.02, 9.0)])] == [sentence.word]
