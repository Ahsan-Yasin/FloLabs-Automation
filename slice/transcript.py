from core.models import EditDecisionList, Word


def remap_transcript(words: list[Word], edl: EditDecisionList) -> list[Word]:
    """Re-map original words onto the trimmed output's timeline (section 4.3).

    Words are assigned to whichever KEEP range they fall inside and shifted by
    that range's cumulative offset in the output; words that were cut are
    dropped from the clean transcript entirely.
    """
    clean: list[Word] = []
    offset = 0.0

    for r in edl.ranges:
        for w in words:
            if w.start >= r.start and w.end <= r.end:
                clean.append(
                    Word(
                        word=w.word,
                        start=w.start - r.start + offset,
                        end=w.end - r.start + offset,
                        speaker=w.speaker,
                        overlap_candidate=w.overlap_candidate,
                    )
                )
        offset += r.end - r.start

    clean.sort(key=lambda w: w.start)
    return clean
