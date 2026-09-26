"""M1 regression gate: re-render the real 98-minute meeting (job 7b93…) with
the v2 renderer and prove the output timeline is exact.

The v1 output of this job is 5332.65 s although its EDL keeps 5254.5 s: stream
copy started every clip at the previous keyframe and put ~78 s of removed
content back. This script rebuilds the EDL on the frame grid from the same
keep ranges and transcript, renders it, and checks:

1. video frames in the output == Σ kept frames (exactly);
2. audio duration == Σ kept frames / fps (within one AAC frame);
3. content: at sample points spread over the whole output, the output frame
   is visually the source frame the formula out(t) = t - s_i + O_i predicts
   (and, for contrast, how far the v1 output was off at the same points).

Nothing in the fixture folder is modified; all output goes to --out.

    .venv/Scripts/python.exe tools/regress_7b93.py --out <scratch dir> [--cut-silence]

By default the EDL has no silence cuts (the gate's historical baseline).
--cut-silence also takes out the pauses nobody speaks in, measured in the
source audio exactly as a job does (slice/silence.py), so the same checks
run on an EDL with many more, shorter cuts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import get_settings
from core.models import Decision, Word
from core.timeline import fade_frames_for
from edl.builder import build_edl, complement
from slice.ffmpeg_wrapper import extract_audio_flac
from slice.pipeline import render_cleaned
from slice.profile import probe_header, probe_media
from slice.silence import SilenceParams, find_silences, reach_end
from slice.transcript import remap_transcript

JOB = ROOT / "storage" / "jobs" / "7b93aa4cd7ef436895213e6fff9365a3"
SOURCE = ROOT / "storage" / "videos" / "50c55c4c8dd64542869bc4e6f30e69e5.mp4"


def grab(path: Path, frame: int, fps: Fraction, w: int, h: int) -> bytes:
    """Decode exactly frame `frame` (seek half a frame early) as 64x40 gray."""
    t = float((Fraction(frame) - Fraction(1, 2)) / fps) if frame > 0 else 0.0
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.6f}", "-i", str(path), "-frames:v", "1",
         "-vf", "scale=64:40,format=gray", "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    ).stdout


def mad(a: bytes, b: bytes) -> float:
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--samples", type=int, default=24)
    ap.add_argument("--cut-silence", action="store_true", help="also cut the pauses nobody speaks in, as jobs do")
    args = ap.parse_args()
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    media = probe_media(SOURCE)
    fps = media.fps
    d = fade_frames_for(fps, 0.5)
    old_edl = json.loads((JOB / "edl.json").read_text(encoding="utf-8"))
    words = [Word(**w) for w in json.loads((JOB / "transcript.json").read_text(encoding="utf-8"))]
    decisions = [Decision(start=r["start"], end=r["end"], decision="keep") for r in old_edl["ranges"]]
    silences = []
    if args.cut_silence:
        settings = get_settings()
        span = (min(w.start for w in words), max(w.end for w in words))  # as pipeline._detect_silences
        found = find_silences(SOURCE, out_dir / "silences.json", SilenceParams.from_settings(settings),
                              media.video_duration, span=span)
        silences = reach_end(found.ranges, found.audio_s, media.video_duration)
        print(f"silences: threshold {found.threshold_db} dBFS, {len(found.ranges)} >= {settings.silence_min_s} s "
              f"({found.total_s:.1f} s)")
    edl = build_edl(decisions, words, media.video_duration, fps=fps, fade_frames=d, total_frames=media.total_frames,
                    silences=silences)
    if args.cut_silence:
        print(f"silence cuts in the EDL: {len(edl.silence_cuts)} ({sum(e - s for s, e in edl.silence_cuts):.1f} s)")
    removed = complement(edl)
    ranges = [(r.start_frame, r.end_frame) for r in edl.ranges]
    expected = sum(e - s for s, e in ranges)
    print(f"source: {media.width}x{media.height} @ {fps} fps, {media.total_frames} frames, "
          f"audio {media.audio_channels}ch {media.audio_sample_rate} Hz")
    print(f"v1 EDL: {len(old_edl['ranges'])} ranges, {sum(r['end'] - r['start'] for r in old_edl['ranges']):.3f} s kept")
    print(f"v2 EDL: {len(ranges)} ranges, fade {d} frames, {expected} frames = {float(expected / fps):.3f} s kept; "
          f"{edl.merged_gap_count} short gaps merged back ({edl.merged_gap_seconds} s); "
          f"{len(removed)} removed ranges ({sum(1 for r in removed if r.tier == 'video')} long enough for removed.mp4)")
    (out_dir / "edl_v2.json").write_text(edl.model_dump_json(indent=2), encoding="utf-8")

    t0 = time.monotonic()
    flac = out_dir / "audio.flac"
    extract_audio_flac(SOURCE, flac, media.audio_channels, media.duration)
    t_flac = time.monotonic() - t0
    out = out_dir / "cleaned.mp4"
    last = [0.0]

    def progress(c, t):
        now = time.monotonic()
        if now - last[0] > 20 or c == t:
            print(f"  render step {c}/{t} ({now - t0:.0f} s)", flush=True)
            last[0] = now

    manifest = render_cleaned(SOURCE, flac, edl, media, out, out_dir / "work", on_progress=progress)
    took = time.monotonic() - t0
    flac.unlink(missing_ok=True)
    (out_dir / "render_manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    header = probe_header(out)

    ok_frames = header.video_frames == expected
    want_audio = float(Fraction(expected) / fps)
    ok_audio = header.audio_duration is not None and abs(header.audio_duration - want_audio) <= 2048 / 48000
    print(f"render: {took:.0f} s total (FLAC {t_flac:.0f} s), parts {len(manifest.parts)}, timings {manifest.timings_s}")
    print(f"output: {header.video_frames} frames (expected {expected}) -> {'EXACT' if ok_frames else 'MISMATCH'}")
    print(f"output audio: {header.audio_duration} s (expected {want_audio:.6f}) -> {'OK' if ok_audio else 'MISMATCH'}")
    print(f"size: {out.stat().st_size / 1e6:.1f} MB")

    # content check at points spread over the output, away from dissolves
    h = d // 2
    offsets, run = [], 0
    for s, e in ranges:
        offsets.append(run)
        run += e - s
    old_ranges = [(r["start"], r["end"]) for r in old_edl["ranges"]]
    old_offsets, orun = [], 0.0
    for s, e in old_ranges:
        old_offsets.append(orun)
        orun += e - s
    old_out = JOB / "output.mp4"
    rows = []
    for k in range(args.samples):
        target = int(expected * (k + 0.5) / args.samples)
        i = max(j for j in range(len(ranges)) if offsets[j] <= target)
        s, e = ranges[i]
        p = min(max(target, offsets[i] + h + 2), offsets[i] + (e - s) - h - 3)
        src_frame = s + (p - offsets[i])
        src_img = grab(SOURCE, src_frame, fps, media.width, media.height)
        new_img = grab(out, p, fps, media.width, media.height)
        # neighbours, to show the match is frame-accurate and not just "similar scene"
        near = min(mad(src_img, grab(SOURCE, src_frame + dd, fps, media.width, media.height)) for dd in (-15, 15))
        row = {"out_s": round(float(p / fps), 2), "src_s": round(float(src_frame / fps), 2),
               "v2_diff": round(mad(src_img, new_img), 2), "src_vs_±0.5s": round(near, 2)}
        # where the v1 output shows the same source moment according to ITS edl
        src_t = float(src_frame / fps)
        oi = next((j for j, (os_, oe) in enumerate(old_ranges) if os_ <= src_t < oe), None)
        if oi is not None and old_out.exists():
            old_t = src_t - old_ranges[oi][0] + old_offsets[oi]
            row["v1_diff"] = round(mad(src_img, grab(old_out, round(old_t * float(fps)), fps, 0, 0)), 2)
        rows.append(row)
    print("content check (mean abs luma diff, 0-255; lower = same picture):")
    for r in rows:
        print("  ", r)
    ok_content = all(r["v2_diff"] < 6 for r in rows)
    print(f"content: {'MATCH' if ok_content else 'CHECK MANUALLY'} (v2 max diff {max(r['v2_diff'] for r in rows)})")

    clean = remap_transcript(words, manifest=manifest)
    print(f"clean transcript: {len(clean)} of {len(words)} words kept; last word ends at {clean[-1].end:.2f} s "
          f"of {want_audio:.2f} s")
    (out_dir / "result.json").write_text(json.dumps({
        "expected_frames": expected, "measured_frames": header.video_frames, "audio_s": header.audio_duration,
        "render_s": took, "content": rows, "ok": ok_frames and ok_audio and ok_content,
    }, indent=2), encoding="utf-8")
    print("GATE PASSED" if ok_frames and ok_audio and ok_content else "GATE FAILED")
    return 0 if ok_frames and ok_audio and ok_content else 1


if __name__ == "__main__":
    raise SystemExit(main())
