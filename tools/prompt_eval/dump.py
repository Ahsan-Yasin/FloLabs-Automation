"""Print a source's sentences for reading/labelling, with timing gaps and the
decisions a local job saved (e.g. Claude Haiku's), if any.

    python tools/prompt_eval/dump.py job:794aa22c [--from 0] [--to 200] [--gap 4]
    python tools/prompt_eval/dump.py raw:zoom-design-team-2026-09-30 --out file.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources import load_saved_judgments, load_segments


def fmt(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--from", dest="lo", type=int, default=0)
    ap.add_argument("--to", dest="hi", type=int, default=None)
    ap.add_argument("--gap", type=float, default=4.0, help="mark pauses at least this long")
    ap.add_argument("--out")
    args = ap.parse_args()
    segs = load_segments(args.source)
    saved = load_saved_judgments(args.source)
    dec = {}
    if saved and len(saved[1]) == len(segs):
        dec = {j.index: j for j in saved[1]}
    hi = len(segs) if args.hi is None else min(args.hi, len(segs))
    lines = [f"# {args.source}: {len(segs)} sentences, {fmt(segs[0].start)}-{fmt(segs[-1].end)}"
             + (f", saved decisions by {saved[0]}" if dec else "")]
    prev_end = None
    prev_spk = None
    for i in range(args.lo, hi):
        s = segs[i]
        if prev_end is not None and s.start - prev_end >= args.gap:
            lines.append(f"      ---- {s.start - prev_end:.0f}s silence ----")
        j = dec.get(i)
        d = f"  || {j.decision[0]} {j.highlight_score} {j.removal_category if j.decision == 'remove' else ''}" \
            f"{' ' + j.highlight_category if j.highlight_category != 'none' else ''}" if j else ""
        spk = s.speaker if s.speaker != prev_spk else "\""
        ov = " (ov)" if s.overlap_candidate else ""
        lines.append(f"{i:5d} {fmt(s.start)} {spk}: {s.text}{ov}{d}")
        prev_end, prev_spk = s.end, s.speaker
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {len(lines)} lines to {args.out}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()
