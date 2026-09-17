from core.models import Segment, Word


def build_segments(words: list[Word]) -> list[Segment]:
    """Collapse word-level ASR output into contiguous speaker-turn segments.

    The LLM edit-decision pass (section 3.3) reasons over turns, not raw
    words — cheaper and gives it the actual unit of "did this add content".
    """
    segments: list[Segment] = []
    current: list[Word] = []

    def flush() -> None:
        if not current:
            return
        segments.append(
            Segment(
                speaker=current[0].speaker,
                start=current[0].start,
                end=current[-1].end,
                text=" ".join(w.word for w in current).strip(),
                overlap_candidate=any(w.overlap_candidate for w in current),
            )
        )

    for word in words:
        if current and word.speaker != current[-1].speaker:
            flush()
            current = []
        current.append(word)
    flush()

    return segments
