import bisect

from core.models import EditDecisionList, RenderManifest, Word
from core.timeline import parse_rate


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
    words: list[Word], edl: EditDecisionList | None = None, manifest: RenderManifest | None = None
) -> list[Word]:
    """Re-map original words onto the rendered output's timeline (section 4.3).

    out(t) = t - s_i + O_i for a word in kept range i. A word belongs to the
    kept range containing its midpoint (so a boundary that frame-snapping moved
    by a fraction of a frame can't silently drop it) and its start/end are
    clamped into that range. Words whose midpoint was cut are dropped. Prefer
    passing the RenderManifest: it is the timeline that was actually rendered.
    """
    if manifest is None and edl is None:
        raise ValueError("remap_transcript needs an EDL or a RenderManifest")
    spans = [(float(s), float(e), float(o)) for s, e, o in _kept_spans(edl, manifest)]
    starts = [s for s, _, _ in spans]
    clean: list[Word] = []
    for w in words:
        mid = (w.start + w.end) / 2
        i = bisect.bisect_right(starts, mid) - 1
        if i < 0:
            continue
        s, e, o = spans[i]
        if not (s <= mid < e):
            continue
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
