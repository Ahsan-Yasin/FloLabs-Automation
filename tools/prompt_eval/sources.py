"""Load a real meeting's sentences exactly the way the app builds them.

Source ids:
- "job:<job id or prefix>"  a local job. segments.json when it exists (the
  exact sentences that job judged); otherwise its cue-level transcript.json
  regrouped with transcribe.native._group_into_sentences (what the app does
  with YouTube captions / Zoom cues).
- "raw:<name>"              tools/prompt_eval/raw/<name>.vtt (a Zoom
  transcript fetched by fetch_zoom_transcripts.py), parsed with
  transcribe.native.parse_subtitle_text (the app's Zoom path).

Overlap flags are set the way pipeline._transcribe sets them
(segments_to_words + flag_overlaps), unless the job's segments.json already
carries them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.models import Segment, SegmentJudgment, Word
from transcribe.native import (
    _group_into_sentences,
    parse_subtitle_text,
    segments_to_words,
)
from transcribe.overlap import flag_overlaps

JOBS = REPO / "storage" / "jobs"
RAW = Path(__file__).resolve().parent / "raw"


def _job_dir(ref: str) -> Path:
    matches = [d for d in JOBS.iterdir() if d.is_dir() and d.name.startswith(ref)]
    if len(matches) != 1:
        raise SystemExit(f"job {ref!r}: {len(matches)} matching folders")
    return matches[0]


def _with_overlaps(segments: list[Segment]) -> list[Segment]:
    words = flag_overlaps(segments_to_words(segments))
    for seg, w in zip(segments, words, strict=True):
        seg.overlap_candidate = w.overlap_candidate
    return segments


def load_segments(source: str) -> list[Segment]:
    kind, _, ref = source.partition(":")
    if kind == "job":
        d = _job_dir(ref)
        seg_path = d / "segments.json"
        if seg_path.exists():
            return [Segment.model_validate(s) for s in json.loads(seg_path.read_text(encoding="utf-8"))]
        cues = [Word.model_validate(w) for w in json.loads((d / "transcript.json").read_text(encoding="utf-8"))]
        cues.sort(key=lambda w: w.start)
        return _with_overlaps(_group_into_sentences(cues))
    if kind == "raw":
        text = (RAW / f"{ref}.vtt").read_text(encoding="utf-8-sig", errors="replace")
        _, segments = parse_subtitle_text(text)
        return _with_overlaps(segments)
    raise SystemExit(f"unknown source {source!r}")


def load_saved_judgments(source: str) -> tuple[str, list[SegmentJudgment]] | None:
    """(model, judgments) a local job saved in decisions.json (e.g. Claude
    Haiku's), aligned with load_segments(source); None when there are none."""
    kind, _, ref = source.partition(":")
    if kind != "job":
        return None
    path = _job_dir(ref) / "decisions.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("model", "?"), [SegmentJudgment.model_validate(j) for j in data.get("judgments", [])]
