import bisect

from core.models import EditDecisionList, RenderManifest, Word
from core.timeline import parse_rate

# less than this of a word in the output counts as none of it (float noise
# between the frame-exact kept and removed ranges)
MIN_OVERLAP_S = 1e-3


def _kept_spans(edl: EditDecisionList | None, manifest: RenderManifest | None) -> list[tuple[float, float, float]]:
    """(src_start_s, src_end_s, out_start_s) for each kept range."""
    if manifest is not None:
        fps = parse_rate(manifest.fps)
        return [
            (p.src_start_frame / fps, p.src_end_frame / fps, p.out_start_frame / fps)
            for p in manifest.pieces
        ]
    spans, offset = [], 0.0
    for r in edl.ranges:
        spans.append((r.start, r.end, offset))
        offset += r.end - r.start
    return spans


def remap_transcript(
    words: list[Word],
    edl: EditDecisionList | None = None,
    manifest: RenderManifest | None = None,
    silence: list[tuple[float, float]] | None = None,
) -> list[Word]:
    """Re-map original words onto the rendered output's timeline (section 4.3).

    out(t) = t - s_i + O_i for a word in kept range i. A word belongs to the
    kept range containing its midpoint (so a boundary that frame-snapping moved
    by a fraction of a frame can't silently drop it) and its start/end are
    clamped into that range. Words whose midpoint was cut are dropped —
    except when the midpoint is in a `silence` (source ranges cut only
    because nobody spoke there): a caption "word" is a whole sentence whose
    timing spans its pauses, so it was still said, in the kept range it
    overlaps most (report/text.removed_entries leaves it out to match).
    Prefer passing the RenderManifest: it is the timeline that was actually
    rendered.
    """
    if manifest is None and edl is None:
        raise ValueError("remap_transcript needs an EDL or a RenderManifest")
    spans = [(float(s), float(e), float(o)) for s, e, o in _kept_spans(edl, manifest)]
    starts = [s for s, _, _ in spans]
    silence = merge_spans(silence or [])
    silence_starts = [s for s, _ in silence]
    clean: list[Word] = []
    for w in words:
        mid = (w.start + w.end) / 2
        i = bisect.bisect_right(starts, mid) - 1
        if i < 0 or not (spans[i][0] <= mid < spans[i][1]):
            k = bisect.bisect_right(silence_starts, mid) - 1
            if k < 0 or not (silence[k][0] <= mid < silence[k][1]):
                continue
            i = _most_overlap(w, spans, starts)
            if i is None:
                continue
        s, e, o = spans[i]
        start = min(max(w.start, s), e)
        end = min(max(w.end, s), e)
        clean.append(
            Word(
                word=w.word,
                start=round(start - s + o, 6),
                end=round(end - s + o, 6),
                speaker=w.speaker,
                overlap_candidate=w.overlap_candidate,
            )
        )
    clean.sort(key=lambda w: w.start)
    return clean


def merge_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sorted, with overlapping spans merged, so a bisect on the starts finds
    the one holding a time (the pause cuts and the frame-rounded pure-silence
    ranges overlap)."""
    merged: list[tuple[float, float]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _most_overlap(w: Word, spans: list[tuple[float, float, float]], starts: list[float]) -> int | None:
    """Index of the kept span `w` overlaps most, or None if it overlaps none."""
    best, best_overlap = None, MIN_OVERLAP_S
    i = max(0, bisect.bisect_right(starts, w.start) - 1)
    while i < len(spans) and spans[i][0] < w.end:
        overlap = min(w.end, spans[i][1]) - max(w.start, spans[i][0])
        if overlap > best_overlap:
            best, best_overlap = i, overlap
        i += 1
    return best
