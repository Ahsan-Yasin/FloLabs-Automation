"""M4 regression: build the complete deliverable (final.mp4, removed.mp4,
highlights.mp4, shorts, transcripts, report.pdf, manifest.json, bundle.zip)
for the 98-minute regression meeting — with ZERO LLM calls.

It reuses the decisions and picks of an earlier `tools/decide_7b93.py` run
(its decisions.json + selection.json) and that run's chapters, then runs the
real selection, rendering and delivery code. Nothing in the fixture folder is
touched; everything is written to --out.

    .venv/Scripts/python.exe tools/deliver_7b93.py --decide-dir <decide_7b93 --out dir> --out <scratch dir>

--force-shorts N renders the N best reel moments as shorts when the meeting
itself yields none (the regression meeting has no funny moments), so the
shorts renderer is exercised on real footage.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import get_settings
from core.models import Chapter, JobOptions, JobRecord, Moment, ShortClip, Word
from core.timeline import fade_frames_for
from decide.chapters import ChapterResult
from decide.gemini_client import LazyCaller, judge_segments
from decide.prompts import REMOVAL_LABELS
from decide.repair import repair_fragments
from deliver import DeliverInputs, deliver
from edl.builder import build_edl, complement
from outputs import RenderInputs
from pipeline import _select, label_removed
from slice.profile import probe_media
from transcribe.native import _group_into_sentences, segments_to_words
from transcribe.overlap import flag_overlaps

JOB = ROOT / "storage" / "jobs" / "7b93aa4cd7ef436895213e6fff9365a3"
SOURCE = ROOT / "storage" / "videos" / "50c55c4c8dd64542869bc4e6f30e69e5.mp4"


class NoCalls(LazyCaller):
    """Any attempt to reach the LLM is a bug in this tool (the saved decisions
    no longer match the prompt): fail instead of spending tokens."""

    def __init__(self, model: str) -> None:
        super().__init__()
        # the model is part of the decisions fingerprint: replay the saved run's
        # answers whichever provider/model .env selects now
        self.model = model

    def generate(self, *a, **k):
        raise RuntimeError("an LLM call was attempted; re-run tools/decide_7b93.py first")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decide-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--force-shorts", type=int, default=2)
    ap.add_argument("--title", default="Weekly engineering sync")
    args = ap.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    settings = get_settings()

    cues = [Word(**w) for w in json.loads((JOB / "transcript.json").read_text(encoding="utf-8"))]
    segments = _group_into_sentences(cues)
    words = flag_overlaps(segments_to_words(segments))
    decisions = out / "decisions.json"
    shutil.copyfile(args.decide_dir / "decisions.json", decisions)
    saved_model = json.loads(decisions.read_text(encoding="utf-8"))["model"]
    judgments = judge_segments(segments, caller=NoCalls(saved_model), persist_path=decisions)
    judgments, _ = repair_fragments(judgments)
    saved = json.loads((args.decide_dir / "selection.json").read_text(encoding="utf-8"))
    moments = [Moment.model_validate(m) for m in saved["moments"]]

    media = probe_media(SOURCE)
    fade = fade_frames_for(media.fps, settings.transition_target_s)
    duration = media.video_duration
    edl = build_edl([j.to_decision() for j in judgments], words, duration, fps=media.fps, fade_frames=fade,
                    total_frames=media.total_frames)
    removed = complement(edl)
    label_removed(removed, judgments, words)
    job = JobRecord(job_id="regress-7b93", title=args.title, options=JobOptions(), source_path=str(SOURCE),
                    transcript_source="uploaded_transcript")
    reel, reel_edl, shorts = _select(job, out, judgments, moments, words, media, fade, duration, job.options,
                                     settings)
    if not shorts and args.force_shorts:
        best = sorted(reel, key=lambda m: -m.score)[: args.force_shorts]
        shorts = [ShortClip(index=i, moment_id=m.id, start=m.start, end=min(m.end, m.start + 60), score=m.score,
                            category=m.category, title=m.title, hook=m.hook) for i, m in enumerate(best, 1)]
        print(f"forcing {len(shorts)} shorts from the reel (the meeting has no short-worthy moments)")
    chapters = [Chapter(start=float(c["t"]), title=c["title"]) for c in saved.get("chapters", [])]

    t0 = time.monotonic()
    last = [""]

    def update(j: JobRecord) -> None:
        line = f"{j.status.value} {j.progress_current}/{j.progress_total}"
        if line != last[0]:
            print(f"  [{time.monotonic() - t0:6.0f}s] {line}", flush=True)
            last[0] = line

    deliver(job, update, DeliverInputs(
        render=RenderInputs(job_dir=out, source=SOURCE, media=media, edl=edl, removed=removed, reel_edl=reel_edl,
                            shorts=shorts, words=words, title=job.title),
        reel=reel,
        chapter_fn=lambda clean_words, cleaned_s: ChapterResult(chapters, bool(chapters), ["none saved"]),
    ))
    (out / "job.json").write_text(job.model_dump_json(indent=2), encoding="utf-8")
    print(f"done in {time.monotonic() - t0:.0f}s: final {job.artifacts['final.mp4'].duration_s:.1f}s, "
          f"cleaned starts at {job.final_offset_s:.3f}s, bundle {job.bundle_bytes / 1024**2:.1f} MB")
    for name, a in job.artifacts.items():
        size = f"{a.bytes / 1024**2:8.1f} MB" if a.bytes else " " * 11
        dur = f"{a.duration_s:8.1f}s" if a.duration_s else " " * 9
        print(f"  {a.status:7} {size} {dur}  {name} {a.reason}")
    for w in job.warnings:
        print(f"  warning: {w}")
    print("removal labels:", sorted({r.reason for r in removed}), "known:", len(REMOVAL_LABELS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
