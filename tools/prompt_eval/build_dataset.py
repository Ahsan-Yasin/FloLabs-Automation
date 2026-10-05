"""Turn datasets/gold.txt into one JSON file per window plus datasets/split.json.

    python tools/prompt_eval/build_dataset.py

Each window is a full contiguous stretch of a real transcript, unchanged (the
sentences come from sources.load_segments, i.e. exactly what the app judges).
datasets/windows/<id>.json holds the sentences (re-indexed from 0, the source
index kept as "src") and one gold record per sentence.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from sources import load_saved_judgments, load_segments

GOLD = HERE / "datasets" / "gold.txt"
OUT = HERE / "datasets" / "windows"
SPLIT = HERE / "datasets" / "split.json"

REMOVAL = {"fill", "greet", "intro", "house", "xtalk", "tang", "rep", "dead"}
HIGHLIGHT = {"fun", "arch", "feat", "idea", "dec", "ins"}


def ranges(text: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        a, _, b = part.partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    return out


def moments(text: str) -> list[list[int]]:
    text = text.strip()
    if not text or text == "none":
        return []
    out = []
    for part in text.split(";"):
        a, _, b = part.strip().partition("-")
        out.append([int(a), int(b or a)])
    return out


def parse_gold(path: Path = GOLD) -> list[dict]:
    windows: list[dict] = []
    cur: dict | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[(\w+)\]$", line)
        if m:
            cur = {"id": m.group(1), "rm": [], "hl": []}
            windows.append(cur)
            continue
        if cur is None:
            raise SystemExit(f"line outside a window: {line}")
        if line.startswith("rm "):
            idx, codes = line[3:].split(":", 1)
            cur["rm"].append((ranges(idx), [c.strip() for c in codes.split(",") if c.strip()]))
        elif line.startswith("hl "):
            idx, rest = line[3:].split(":", 1)
            band, *cats = rest.split()
            cur["hl"].append((ranges(idx), int(band), [c for c in ",".join(cats).split(",") if c]))
        else:
            key, _, value = line.partition("=")
            cur[key.strip()] = value.strip()
    return windows


def build_window(w: dict) -> dict:
    lo, _, hi = w["range"].partition("-")
    lo, hi = int(lo), int(hi or lo)
    segs = load_segments(w["source"])[lo : hi + 1]
    span = range(lo, hi + 1)
    keep, must, amb = set(ranges(w.get("keep", ""))), set(ranges(w.get("must", ""))), set(ranges(w.get("amb", "")))
    for name, s in (("keep", keep), ("must", must), ("amb", amb)):
        bad = sorted(i for i in s if i not in span)
        if bad:
            raise SystemExit(f"{w['id']}: {name} outside the range: {bad[:5]}")
    if not must <= keep:
        raise SystemExit(f"{w['id']}: must not in keep: {sorted(must - keep)[:5]}")
    ok_codes: dict[int, list[str]] = {}
    for idx, codes in w["rm"]:
        unknown = set(codes) - REMOVAL
        if unknown:
            raise SystemExit(f"{w['id']}: unknown removal codes {unknown}")
        for i in idx:
            ok_codes.setdefault(i, [])
            ok_codes[i] += [c for c in codes if c not in ok_codes[i]]
    band: dict[int, tuple[int, list[str]]] = {}
    for idx, b, cats in w["hl"]:
        if set(cats) - HIGHLIGHT:
            raise SystemExit(f"{w['id']}: unknown highlight codes {set(cats) - HIGHLIGHT}")
        for i in idx:
            band[i] = (b, cats)
    saved = load_saved_judgments(w["source"])
    prior = {}
    if saved and len(saved[1]) == len(load_segments(w["source"])):
        prior = {j.index: j for j in saved[1]}
    sentences, gold = [], []
    for n, (src, s) in enumerate(zip(span, segs, strict=True)):
        sentences.append({"i": n, "src": src, "start": s.start, "end": s.end, "speaker": s.speaker,
                          "text": s.text, "overlap_candidate": bool(getattr(s, "overlap_candidate", False))})
        b, cats = band.get(src, (0, []))
        g = {"i": n, "expect": "keep" if src in keep else "remove", "must_keep": src in must,
             "ambiguous": src in amb, "ok_removal_codes": [] if src in keep else ok_codes.get(src, sorted(REMOVAL)),
             "highlight_band": b, "highlight_codes": cats}
        p = prior.get(src)
        if p is not None:
            g["prior"] = {"model": saved[0], "decision": p.decision, "score": p.highlight_score,
                          "removal": p.removal_category, "highlight": p.highlight_category}
        gold.append(g)
    rel = [[a - lo, b - lo] for a, b in moments(w.get("top", ""))]
    short = [[a - lo, b - lo] for a, b in moments(w.get("short", ""))]
    return {
        "id": w["id"], "source": w["source"], "source_range": [lo, hi], "split": w["split"],
        "minutes": round((segs[-1].end - segs[0].start) / 60, 2), "hard": w.get("hard", ""),
        "sentences": sentences, "gold": gold, "top_moments": rel, "short_worthy": short,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    split: dict[str, list[str]] = {"train": [], "test": []}
    for w in parse_gold():
        data = build_window(w)
        (OUT / f"{w['id']}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        split[w["split"]].append(w["id"])
        n = len(data["sentences"])
        kept = sum(g["expect"] == "keep" for g in data["gold"])
        print(f"{w['id']:38s} {w['split']:5s} {data['minutes']:5.2f} min {n:3d} sentences "
              f"{kept:3d} keep {sum(g['must_keep'] for g in data['gold']):3d} must")
    SPLIT.write_text(json.dumps(split, indent=1), encoding="utf-8")
    print(f"split: {len(split['train'])} train, {len(split['test'])} test -> {SPLIT}")


if __name__ == "__main__":
    main()
