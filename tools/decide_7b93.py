"""M3 regression: run the v2 decide stage (real LLM calls, with the provider
and model configured in .env) on the 98-minute regression meeting's cached
transcript and write everything a reviewer needs.

No rendering; nothing in the fixture folder is modified. Decisions are saved
incrementally in --out, so a re-run with the same prompts makes no new calls.

    .venv/Scripts/python.exe tools/decide_7b93.py --out <scratch dir>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import get_settings
from core.models import Decision, Word
from core.timeline import fade_frames_for, fmt_clock
from decide.chapters import finalize_chapters, format_chapters, generate_chapters
from decide.gemini_client import LazyCaller, judge_segments, rerank_moments
from decide.repair import repair_fragments
from edl.builder import build_edl, complement
from edl.highlights import (
    calibrate_by_rank,
    candidate_moments,
    select_highlights,
    select_shorts,
)
from slice.transcript import remap_transcript
from transcribe.native import _group_into_sentences, segments_to_words
from transcribe.overlap import flag_overlaps

JOB = ROOT / "storage" / "jobs" / "7b93aa4cd7ef436895213e6fff9365a3"
FPS = Fraction(30)
TOTAL_FRAMES = 177322
DURATION = TOTAL_FRAMES / 30


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--chapters-from", type=Path, default=None,
                    help="reuse the chapters of an earlier run's selection.json instead of a chapters call "
                         "(saves ~40k tokens while tuning the re-rank)")
    args = ap.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    settings = get_settings()

    cues = [Word(**w) for w in json.loads((JOB / "transcript.json").read_text(encoding="utf-8"))]
    segments = _group_into_sentences(cues)  # what the live pipeline feeds the model
    words = flag_overlaps(segments_to_words(segments))
    print(f"{len(cues)} caption cues -> {len(segments)} sentences")

    caller = LazyCaller()
    t0 = time.monotonic()
    judgments = judge_segments(
        segments, caller=caller, persist_path=out / "decisions.json",
        on_progress=lambda c, t: print(f"  judged {c}/{t} ({time.monotonic() - t0:.0f}s)", flush=True),
    )
    t_judge = time.monotonic() - t0
    judgments, repaired = repair_fragments(judgments)
    usage_decide = dict(caller.usage.as_dict())

    candidates = candidate_moments(judgments, floor=settings.highlights_candidate_floor,
                                   funny_floor=settings.highlights_funny_floor,
                                   join_gap_s=settings.highlights_join_gap_s,
                                   max_candidates=settings.rerank_max_candidates)
    t1 = time.monotonic()
    rerank = rerank_moments(candidates, judgments, caller=caller)
    t_rerank = time.monotonic() - t1
    moments = calibrate_by_rank(rerank.moments, settings.highlights_min_raw_score)

    d = fade_frames_for(FPS, 0.5)
    edl = build_edl([j.to_decision() for j in judgments], words, DURATION, fps=FPS, fade_frames=d,
                    total_frames=TOTAL_FRAMES)
    removed = complement(edl)
    reel = select_highlights(moments, duration_s=DURATION, target_s=settings.highlights_target_s,
                             max_fraction=settings.highlights_max_fraction, min_total_s=settings.highlights_min_s,
                             min_moment_s=settings.highlights_min_moment_s, min_score=settings.highlights_min_score,
                             max_share_per_window=settings.highlights_max_share_per_window,
                             window_s=settings.highlights_diversity_window_s,
                             max_funny_share=settings.highlights_max_funny_share, judgments=judgments)
    reel_edl = build_edl([Decision(start=m.start, end=m.end, decision="keep") for m in reel], words, DURATION,
                         fps=FPS, fade_frames=d, min_segment_s=settings.highlights_min_moment_s,
                         full_video_fallback=False, total_frames=TOTAL_FRAMES, min_gap_s=2.0) if reel else None
    shorts = select_shorts(moments, judgments, count=settings.shorts_count, min_s=settings.shorts_min_s,
                           max_s=settings.shorts_max_s, preferred_categories=settings.shorts_categories)

    kept_s = sum(r.end - r.start for r in edl.ranges)
    clean = remap_transcript(words, edl=edl)
    t2 = time.monotonic()
    if args.chapters_from:
        saved = json.loads(args.chapters_from.read_text(encoding="utf-8")).get("chapters", [])
        entries, problems = [(c["t"], c["title"]) for c in saved], ["none saved"]
    else:
        caller.start_stage("chapters", settings.chapters_max_wall_s)
        chapters = generate_chapters(clean, kept_s, caller=caller)
        entries, problems = (finalize_chapters(chapters.chapters, final_duration_s=kept_s) if chapters.ok
                             else ([], chapters.problems))
    t_chapters = time.monotonic() - t2

    old = json.loads((JOB / "edl.json").read_text(encoding="utf-8"))
    old_kept = sum(r["end"] - r["start"] for r in old["ranges"])
    first_substance = next((j for j in judgments if j.decision == "keep" and j.start > 60), None)
    opening = [j for j in judgments if j.end <= 300]
    cats: dict[str, float] = {}
    for j in judgments:
        if j.decision == "remove":
            cats[j.removal_category] = cats.get(j.removal_category, 0.0) + (j.end - j.start)
    score_hist: dict[int, int] = {}
    for j in judgments:
        score_hist[j.highlight_score] = score_hist.get(j.highlight_score, 0) + 1

    lines = [
        f"sentences: {len(segments)}; judge {t_judge:.0f}s, re-rank {t_rerank:.0f}s, chapters {t_chapters:.0f}s",
        f"LLM usage total: {caller.usage.as_dict()}; judging only: {usage_decide}; fragments repaired: {repaired}",
        (f"source {DURATION / 60:.1f} min; v1 kept {old_kept / 60:.1f} min; v2 keeps {kept_s / 60:.1f} min "
         f"({len(edl.ranges)} ranges, removed {(DURATION - kept_s) / 60:.1f} min in {len(removed)} ranges, "
         f"{edl.merged_gap_count} short gaps put back)"),
        "removed by category (min): " + ", ".join(f"{k} {v / 60:.1f}" for k, v in sorted(cats.items(),
                                                                                     key=lambda kv: -kv[1])),
        (f"opening 5 min: {sum(1 for j in opening if j.decision == 'remove')}/{len(opening)} sentences removed; "
         f"first kept sentence after 1:00 at {fmt_clock(first_substance.start) if first_substance else '-'}"),
        "highlight score histogram: " + ", ".join(f"{k}:{v}" for k, v in sorted(score_hist.items())),
        f"re-rank ok={rerank.ok} {rerank.note}; {len(candidates)} candidates",
        f"reel: {len(reel)} moments, {sum(r.end - r.start for r in reel_edl.ranges) if reel_edl else 0:.0f}s",
    ]
    for m in reel:
        lines.append(f"   REEL {fmt_clock(m.start)}-{fmt_clock(m.end)} [{m.score:.0f} {m.category}] {m.title} — {m.hook}")
    lines.append(f"shorts: {len(shorts)}")
    for s in shorts:
        lines.append(f"   SHORT {fmt_clock(s.start)}-{fmt_clock(s.end)} ({s.end - s.start:.0f}s) "
                     f"[{s.score:.0f} {s.category}] {s.title} — {s.hook}")
    lines.append("chapters (cleaned timeline): " + ("OK" if entries else f"FAILED {problems}"))
    lines += ["   " + ln for ln in format_chapters(entries, kept_s).splitlines()]
    report = "\n".join(lines)
    print(report)
    (out / "report.txt").write_text(report + "\n", encoding="utf-8")

    def seg_text(first, last):
        return " ".join(j.text for j in judgments[first:last + 1])

    (out / "selection.json").write_text(json.dumps({
        "moments": [{**m.model_dump(), "text": seg_text(m.first_index, m.last_index)} for m in moments],
        "reel_ids": [m.id for m in reel],
        "shorts": [{**s.model_dump(),
                    "text": " ".join(j.text for j in judgments if j.start >= s.start and j.end <= s.end)}
                   for s in shorts],
        "chapters": [{"t": t, "title": title} for t, title in entries],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    with (out / "judgments.tsv").open("w", encoding="utf-8") as f:
        f.write("index\tstart\tend\tdecision\tremoval_category\tscore\tcategory\treason\ttext\n")
        for j in judgments:
            f.write(f"{j.index}\t{fmt_clock(j.start)}\t{fmt_clock(j.end)}\t{j.decision}\t{j.removal_category}\t"
                    f"{j.highlight_score}\t{j.highlight_category}\t{j.reason}\t{j.text}\n")
    (out / "clean_transcript.txt").write_text(
        "\n".join(f"[{fmt_clock(w.start)}] {w.word}" for w in clean), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
