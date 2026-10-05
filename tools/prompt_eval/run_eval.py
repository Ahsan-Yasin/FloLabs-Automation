"""Score the decide prompts on the gold windows (real transcript stretches).

    python tools/prompt_eval/run_eval.py --split train|test|all --tag <name> [--rerank] [--max-cost 0.30]
    python tools/prompt_eval/run_eval.py --split all --tag dry --rerank --fake     # no API calls

Each window is judged fresh with decide.gemini_client.judge_segments (the app's
own chunking, context lines, retries and parsing) and, with --rerank, re-ranked
the way pipeline._rerank does it. Results go to results/<tag>.json plus one
per-sentence TSV per window (results/<tag>_<window>.tsv).

Spend: every real run is recorded in spend_ledger.json BEFORE the first call
(with a pre-run estimate) and updated after every call. A run is refused when
its estimate is over --max-cost or would take the ledger over $3.00, and it is
stopped mid-way when its actual cost reaches either limit. --fake runs cost
nothing and are recorded as fake.

Weighted score (0-100), weights chosen so cutting real content hurts most:
  keep_safety   0.30   1 - must_keep_removed / must_keep_total
  removal_f1    0.25   remove-vs-keep F1, positive = remove (ambiguous lines weigh 0.5)
  junk_clean    0.10   1 - junk_kept / clear-junk total
  code_agree    0.05   removed lines whose code is one the editor accepts
  hl_concord    0.15   pairwise order agreement of scores vs gold highlight bands
  top_f1        0.10   gold top moments found by strong predicted moments (score >= 5), and vice versa
  short_f1      0.05   (--rerank only) short-worthy moments vs gold
  minus 2 points per format problem (unparsable answer), at most 10.
A metric that cannot be computed (e.g. short_f1 without --rerank) drops out
and the other weights are renormalised.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

from core.config import get_settings
from core.models import Segment
from decide.gemini_client import (
    LLMUsage,
    judge_segments,
    parse_judge_lines,
    parse_rerank_lines,
    rerank_moments,
)
from decide.pricing import estimate_cost_usd
from decide.prompts import (
    HIGHLIGHT_TO_CODE,
    REMOVAL_CODES,
    judge_prompt,
    rerank_prompt,
)
from edl.highlights import calibrate_by_rank, candidate_moments

DATA = HERE / "datasets"
RESULTS = HERE / "results"
LEDGER = HERE / "spend_ledger.json"
TOTAL_LIMIT_USD = 3.00
STRONG = 5  # a predicted moment counts as a highlight from this score up

WEIGHTS = {"keep_safety": 0.30, "removal_f1": 0.25, "junk_clean": 0.10, "code_agree": 0.05,
           "hl_concord": 0.15, "top_f1": 0.10, "short_f1": 0.05}
CODE_OF = {v: k for k, v in REMOVAL_CODES.items()}

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


# ------------------------------------------------------------------ ledger


def load_ledger() -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    return {"limit_usd": TOTAL_LIMIT_USD, "note": "every real Claude call made by run_eval.py", "runs": []}


def ledger_spent(ledger: dict) -> float:
    return round(sum(r.get("actual_usd") or 0.0 for r in ledger["runs"] if not r.get("fake")), 4)


def save_ledger(ledger: dict) -> None:
    ledger["spent_usd"] = ledger_spent(ledger)
    LEDGER.write_text(json.dumps(ledger, indent=1), encoding="utf-8")


class BudgetExceeded(RuntimeError):
    pass


# ------------------------------------------------------------------ callers


@dataclass
class _Response:
    text: str
    finish_reason: str = "STOP"
    usage: dict = field(default_factory=dict)


class GuardedCaller:
    """Wraps the app's caller: counts format problems, updates the ledger after
    every call and stops before a call once the run's cost hits a limit."""

    def __init__(self, inner, ledger: dict, entry: dict, max_cost: float):
        self.inner, self.ledger, self.entry, self.max_cost = inner, ledger, entry, max_cost
        self.format_problems: list[str] = []
        self.stage = "judge"

    @property
    def model(self) -> str:
        return self.inner.model

    @property
    def usage(self) -> LLMUsage:
        return self.inner.usage

    def cost(self) -> float:
        return estimate_cost_usd(self.inner.usage.as_dict(), self.inner.model) or 0.0

    def generate(self, system_prompt: str, contents: str, *args, **kwargs):
        spent_before = ledger_spent(self.ledger) - (0 if self.entry.get("fake") else (self.entry.get("actual_usd") or 0))
        run_cost = self.cost()
        if run_cost >= self.max_cost or (not self.entry.get("fake") and spent_before + run_cost >= TOTAL_LIMIT_USD):
            raise BudgetExceeded(f"stopped: run cost ${run_cost:.4f} (max ${self.max_cost}), "
                                 f"ledger ${spent_before + run_cost:.4f} of ${TOTAL_LIMIT_USD}")
        response = self.inner.generate(system_prompt, contents, *args, **kwargs)
        self.entry["actual_usd"] = round(self.cost(), 4)
        self.entry["usage"] = self.inner.usage.as_dict()
        save_ledger(self.ledger)
        text = getattr(response, "text", "") or ""
        if self.stage == "judge":
            try:
                parse_judge_lines(text)
            except ValueError as exc:
                self.format_problems.append(f"judge: {exc}")
        else:
            lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("```")]
            bad = len(lines) - len(parse_rerank_lines(text))
            if bad:
                self.format_problems.append(f"rerank: {bad} unparsable line(s)")
        return response


class FakeCaller:
    """Answers from the gold labels with deliberate mistakes (no API): proves
    the metrics, the TSVs and the ledger without spending anything."""

    def __init__(self, prior: bool = False):
        self.model = "claude-sonnet-5-5"
        self.usage = LLMUsage()
        self.gold: list[dict] = []
        self.shorts: list[list[int]] = []
        self.broke_once = False
        self.prior = prior

    def generate(self, system_prompt: str, contents: str, *args, **kwargs):
        if re.search(r"^#\d+ core \d+-\d+", contents, re.MULTILINE):
            text = self._rerank(contents)
        else:
            text = self._judge(contents)
        self.usage.calls += 1
        self.usage.prompt_tokens += (len(system_prompt) + len(contents)) // 4
        self.usage.output_tokens += len(text) // 4
        return _Response(text)

    def _judge(self, contents: str) -> str:
        out, judging = [], False
        for line in contents.splitlines():
            if line.startswith("JUDGE:"):
                judging = True
                continue
            if line.startswith("CONTEXT"):
                judging = False
                continue
            m = re.match(r"^\[(\d+)\]", line)
            if not (judging and m):
                continue
            i = int(m.group(1))
            g = self.gold[i]
            if self.prior:  # replay the decisions the local job saved (e.g. Claude Haiku's)
                p = g["prior"]
                keep = p["decision"] == "keep"
                rcode = "-" if keep else CODE_OF.get(p["removal"], "fill")
                hcode = HIGHLIGHT_TO_CODE.get(p["highlight"], "-")
                out.append(f"{i} {'k' if keep else 'r'} {p['score']} {rcode} {hcode}")
                continue
            keep = g["expect"] == "keep"
            if i % 9 == 4:  # deliberate keep/remove mistakes
                keep = not keep
            score = min(10, g["highlight_band"] * 3 + (i % 2))
            hcode = g["highlight_codes"][0] if g["highlight_codes"] and score >= 3 else "-"
            rcode = "-"
            if not keep:
                ok = g["ok_removal_codes"] or ["fill"]
                rcode = "tang" if i % 5 == 0 else ok[0]  # some code disagreements
            out.append(f"{i} {'k' if keep else 'r'} {score} {rcode} {hcode}")
        if not self.broke_once and out and not self.prior:
            self.broke_once = True  # one malformed answer -> counted, and the app retries
            return "Sure! Here are the judgments:\n" + "\n".join(out)
        return "\n".join(out)

    def _rerank(self, contents: str) -> str:
        out = []
        for block in contents.split("\n\n"):
            m = re.match(r"^#(\d+) core (\d+)-(\d+)", block.strip())
            if not m:
                continue
            cid, a, b = (int(x) for x in m.groups())
            band = max(self.gold[i]["highlight_band"] for i in range(a, b + 1))
            short = any(a <= hi and b >= lo for lo, hi in self.shorts)
            out.append(f"{cid}|{band * 3}|idea|{a}|{b}|{'y' if short else 'n'}|Fake title {cid}|fake hook")
        return "\n".join(out)


# ------------------------------------------------------------------ helpers


def load_windows(split: str) -> list[dict]:
    names = json.loads((DATA / "split.json").read_text(encoding="utf-8"))
    ids = names["train"] + names["test"] if split == "all" else names[split]
    return [json.loads((DATA / "windows" / f"{w}.json").read_text(encoding="utf-8")) for w in ids]


def to_segments(window: dict) -> list[Segment]:
    return [Segment(speaker=s["speaker"], start=s["start"], end=s["end"], text=s["text"],
                    overlap_candidate=s["overlap_candidate"]) for s in window["sentences"]]


def estimate_usd(windows: list[dict], rerank: bool) -> float:
    """Conservative: no cache discount, ~3.5 chars per token, 10 output tokens
    per sentence, x1.3 for retries/splits."""
    s = get_settings()
    size, ctx = max(1, s.gemini_max_segments_per_call), max(0, s.gemini_context_segments)
    sys_judge = len(judge_prompt(s.highlights_criteria))
    sys_rerank = len(rerank_prompt(s.highlights_criteria, s.shorts_criteria))
    tin = tout = 0.0
    for w in windows:
        texts = [len(x["text"]) + len(x["speaker"]) + 8 for x in w["sentences"]]
        n = len(texts)
        for start in range(0, n, size):
            lo, hi = max(0, start - ctx), min(n, start + size + ctx)
            tin += (sys_judge + sum(texts[lo:hi])) / 3.5
            tout += 10 * min(size, n - start)
        if rerank:
            tin += (sys_rerank + sum(texts) * 1.5) / 3.5
            tout += 60 * max(1, n // 8)
    return round(1.3 * (tin * 2.0 + tout * 10.0) / 1e6, 4)


def overlaps(a: list[int], b: list[int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def f1(p: float | None, r: float | None) -> float | None:
    if p is None or r is None:
        return None
    return 0.0 if p + r == 0 else 2 * p * r / (p + r)


def ratio(a: float, b: float) -> float | None:
    return None if b == 0 else a / b


# ------------------------------------------------------------------ scoring


def score_window(w: dict, judgments, moments, reranked: bool) -> tuple[dict, list[str]]:
    gold = w["gold"]
    tp = fp = fn = 0.0
    must_removed, junk_kept, wrong_removed = [], [], []
    code_ok = code_n = 0
    rows = ["i\tsrc\tspeaker\ttext\texpect\tmust\tamb\tok_codes\tband\tpred\tcode\tscore\thl\tverdict\tprior"]
    for g, j in zip(gold, judgments, strict=True):
        wt = 0.5 if g["ambiguous"] else 1.0
        exp_rm, pred_rm = g["expect"] == "remove", j.decision == "remove"
        code = CODE_OF.get(j.removal_category, "-") if pred_rm else "-"
        verdict = "ok"
        if exp_rm and pred_rm:
            tp += wt
            code_n += 1
            if code in g["ok_removal_codes"]:
                code_ok += 1
            else:
                verdict = "code?"
        elif pred_rm:
            fp += wt
            verdict = "MUST_KEEP_REMOVED" if g["must_keep"] else ("removed(amb)" if g["ambiguous"] else "WRONG_REMOVE")
            if g["must_keep"]:
                must_removed.append(f"{w['id']}[{g['i']}] {j.text[:90]}")
            elif not g["ambiguous"]:
                wrong_removed.append(f"{w['id']}[{g['i']}] {j.text[:90]}")
        elif exp_rm:
            fn += wt
            verdict = "kept(amb)" if g["ambiguous"] else "JUNK_KEPT"
            if not g["ambiguous"]:
                junk_kept.append(f"{w['id']}[{g['i']}] {j.text[:90]}")
        p = g.get("prior") or {}
        prior = f"{p.get('decision', '')[:1]} {p.get('score', '')} {p.get('removal', '')}".strip() if p else ""
        rows.append("\t".join(str(x) for x in (
            g["i"], w["sentences"][g["i"]]["src"], j.speaker, j.text.replace("\t", " ")[:160], g["expect"],
            int(g["must_keep"]), int(g["ambiguous"]), ",".join(g["ok_removal_codes"]), g["highlight_band"],
            j.decision, code, j.highlight_score, j.highlight_category, verdict, prior)))
    # highlight order agreement: pairs of sentences with different gold bands
    conc = pairs = 0.0
    for a in range(len(gold)):
        for b in range(a + 1, len(gold)):
            ga, gb = gold[a]["highlight_band"], gold[b]["highlight_band"]
            if ga == gb:
                continue
            pairs += 1
            sa, sb = judgments[a].highlight_score, judgments[b].highlight_score
            if sa == sb:
                conc += 0.5
            elif (sa > sb) == (ga > gb):
                conc += 1
    strong = [m for m in moments if (m.raw_score if m.raw_score is not None else m.score) >= STRONG]
    spans = [[m.first_index, m.last_index] for m in strong]
    top_hit = sum(any(overlaps(t, s) for s in spans) for t in w["top_moments"])
    pred_hit = sum(any(overlaps(s, t) for t in w["top_moments"]) for s in spans)
    shorts = [[m.first_index, m.last_index] for m in moments if m.short_worthy] if reranked else []
    short_hit = sum(any(overlaps(t, s) for s in shorts) for t in w["short_worthy"])
    short_pred_hit = sum(any(overlaps(s, t) for t in w["short_worthy"]) for s in shorts)
    m = {
        "sentences": len(gold), "tp": tp, "fp": fp, "fn": fn,
        "must_keep_total": sum(g["must_keep"] for g in gold), "must_keep_removed": must_removed,
        "junk_total": sum(g["expect"] == "remove" and not g["ambiguous"] for g in gold), "junk_kept": junk_kept,
        "wrong_removed": wrong_removed, "code_ok": code_ok, "code_n": code_n,
        "hl_pairs": pairs, "hl_concordant": conc,
        "top_gold": len(w["top_moments"]), "top_hit": top_hit, "strong_pred": len(spans), "strong_pred_hit": pred_hit,
        "short_gold": len(w["short_worthy"]), "short_hit": short_hit, "short_pred": len(shorts),
        "short_pred_hit": short_pred_hit,
        "max_score": max((j.highlight_score for j in judgments), default=0),
        "kept_pred": sum(j.decision == "keep" for j in judgments),
        "moments": [{"first": mo.first_index, "last": mo.last_index, "score": mo.score, "raw": mo.raw_score,
                     "short": mo.short_worthy, "title": mo.title} for mo in moments],
    }
    return m, rows


def summarize(per: dict[str, dict], reranked: bool, format_problems: int) -> dict:
    tot = lambda k: sum(v[k] for v in per.values())
    tp, fp, fn = tot("tp"), tot("fp"), tot("fn")
    prec, rec = ratio(tp, tp + fp), ratio(tp, tp + fn)
    must_total = tot("must_keep_total")
    must_removed = sum(len(v["must_keep_removed"]) for v in per.values())
    junk_total = tot("junk_total")
    junk_kept = sum(len(v["junk_kept"]) for v in per.values())
    tg, th, sp, sph = tot("top_gold"), tot("top_hit"), tot("strong_pred"), tot("strong_pred_hit")
    top_r, top_p = ratio(th, tg), ratio(sph, sp)
    if tg == 0 and sp == 0:
        top_r = top_p = 1.0
    elif sp == 0:
        top_p = 1.0 if tg == 0 else 0.0
    short_f1 = None
    if reranked:
        sg, sh, spr, sprh = tot("short_gold"), tot("short_hit"), tot("short_pred"), tot("short_pred_hit")
        sr, spp = ratio(sh, sg), ratio(sprh, spr)
        short_f1 = 1.0 if sg == 0 and spr == 0 else f1(sr if sr is not None else 0.0, spp if spp is not None else 0.0)
    metrics = {
        "keep_safety": None if must_total == 0 else 1 - must_removed / must_total,
        "removal_f1": f1(prec, rec),
        "junk_clean": None if junk_total == 0 else 1 - junk_kept / junk_total,
        "code_agree": ratio(tot("code_ok"), tot("code_n")),
        "hl_concord": ratio(tot("hl_concordant"), tot("hl_pairs")),
        "top_f1": f1(top_r, top_p),
        "short_f1": short_f1,
    }
    used = {k: w for k, w in WEIGHTS.items() if metrics[k] is not None}
    base = sum(metrics[k] * w for k, w in used.items()) / sum(used.values())
    score = round(100 * base - min(10, 2 * format_problems), 2)
    return {
        "score": score,
        "metrics": {k: (None if v is None else round(v, 4)) for k, v in metrics.items()},
        "removal_precision": None if prec is None else round(prec, 4),
        "removal_recall": None if rec is None else round(rec, 4),
        "must_keep_removed": must_removed, "must_keep_total": must_total,
        "must_keep_removed_list": [x for v in per.values() for x in v["must_keep_removed"]],
        "junk_kept": junk_kept, "junk_total": junk_total,
        "junk_kept_list": [x for v in per.values() for x in v["junk_kept"]],
        "top_moments": {"gold": tg, "found": th, "strong_predicted": sp, "strong_on_gold": sph},
        "format_problems": format_problems,
        "weights": WEIGHTS,
    }


# ------------------------------------------------------------------ main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["train", "test", "all"], default="train")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--max-cost", type=float, default=0.30)
    ap.add_argument("--fake", action="store_true", help="answer from the gold labels; no API calls")
    ap.add_argument("--prior", action="store_true",
                    help="no API calls: score the decisions the local jobs saved (Claude Haiku baseline); "
                         "only windows from jobs with saved decisions")
    ap.add_argument("--windows", help="comma-separated window ids (subset of the split)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    windows = load_windows(args.split)
    if args.prior:
        args.fake, args.rerank = True, False
        windows = [w for w in windows if all("prior" in g for g in w["gold"])]
    if args.windows:
        keep = set(args.windows.split(","))
        windows = [w for w in windows if w["id"] in keep]
    settings = get_settings()
    estimate = estimate_usd(windows, args.rerank)
    ledger = load_ledger()
    spent = ledger_spent(ledger)
    print(f"{len(windows)} windows, estimate ${estimate:.4f} (max ${args.max_cost:.2f}); "
          f"ledger ${spent:.4f} of ${TOTAL_LIMIT_USD:.2f}")
    if not args.fake:
        if estimate > args.max_cost:
            raise SystemExit(f"refused: estimate ${estimate:.4f} is over --max-cost ${args.max_cost:.2f}")
        if spent + estimate > TOTAL_LIMIT_USD:
            raise SystemExit(f"refused: ${spent:.4f} spent + ${estimate:.4f} would pass ${TOTAL_LIMIT_USD:.2f}")
        if settings.llm_provider != "anthropic":
            raise SystemExit(f"refused: LLM_PROVIDER is {settings.llm_provider!r}, the eval is for Claude")

    entry = {"tag": args.tag, "split": args.split, "rerank": args.rerank, "fake": args.fake,
             "started": datetime.now(UTC).isoformat(timespec="seconds"), "estimate_usd": estimate,
             "max_cost_usd": args.max_cost, "actual_usd": 0.0, "status": "running"}
    ledger["runs"].append(entry)
    save_ledger(ledger)

    if args.fake:
        inner = FakeCaller(prior=args.prior)
    else:
        from decide.gemini_client import make_caller
        inner = make_caller()
        entry["model"] = inner.model
    caller = GuardedCaller(inner, ledger, entry, args.max_cost)
    RESULTS.mkdir(parents=True, exist_ok=True)
    per: dict[str, dict] = {}
    t0 = time.time()
    try:
        for w in windows:
            segs = to_segments(w)
            if args.fake:
                inner.gold, inner.shorts = w["gold"], w["short_worthy"]
            before = dict(caller.usage.as_dict())
            caller.stage = "judge"
            judgments = judge_segments(segs, caller=caller, highlights_criteria=settings.highlights_criteria)
            moments = candidate_moments(judgments, floor=settings.highlights_candidate_floor,
                                        funny_floor=settings.highlights_funny_floor,
                                        join_gap_s=settings.highlights_join_gap_s,
                                        max_candidates=settings.rerank_max_candidates)
            if args.rerank and moments:
                caller.stage = "rerank"
                result = rerank_moments(moments, judgments, caller=caller,
                                        highlights_criteria=settings.highlights_criteria,
                                        shorts_criteria=settings.shorts_criteria)
                moments = calibrate_by_rank(result.moments, settings.highlights_min_raw_score)
            m, rows = score_window(w, judgments, moments, args.rerank)
            after = caller.usage.as_dict()
            m["usage"] = {k: round(after[k] - before.get(k, 0), 1) for k in after}
            per[w["id"]] = m
            (RESULTS / f"{args.tag}_{w['id']}.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            print(f"  {w['id']:38s} must-cut {len(m['must_keep_removed']):2d}/{m['must_keep_total']:2d}  "
                  f"junk-kept {len(m['junk_kept']):2d}/{m['junk_total']:2d}  max score {m['max_score']:2d}  "
                  f"top {m['top_hit']}/{m['top_gold']}  calls {int(m['usage']['calls'])}")
        entry["status"] = "done"
    except BudgetExceeded as exc:
        entry["status"] = "stopped"
        print(str(exc))
    finally:
        entry["actual_usd"] = 0.0 if args.fake else round(caller.cost(), 4)
        entry["usage"] = caller.usage.as_dict()
        if args.fake:
            entry["simulated_usd"] = round(caller.cost(), 4)
        entry["seconds"] = round(time.time() - t0, 1)
        save_ledger(ledger)

    if not per:
        raise SystemExit("no window finished")
    summary = summarize(per, args.rerank, len(caller.format_problems))
    usage = caller.usage.as_dict()
    out = {
        "tag": args.tag, "split": args.split, "rerank": args.rerank, "fake": args.fake,
        "model": caller.model, "when": entry["started"], "status": entry["status"],
        **summary,
        "format_problem_details": caller.format_problems,
        "calls": usage["calls"], "usage": usage,
        "cost_usd": entry.get("simulated_usd", entry["actual_usd"]), "estimate_usd": estimate,
        "ledger_spent_usd": ledger_spent(ledger),
        "windows": per,
    }
    (RESULTS / f"{args.tag}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    met = summary["metrics"]
    print(f"score {summary['score']}  " + "  ".join(f"{k} {'-' if v is None else f'{v:.3f}'}" for k, v in met.items()))
    print(f"removal P {summary['removal_precision']} R {summary['removal_recall']}  "
          f"must-keep removed {summary['must_keep_removed']}/{summary['must_keep_total']}  "
          f"junk kept {summary['junk_kept']}/{summary['junk_total']}  format problems {summary['format_problems']}")
    print(f"calls {usage['calls']}  in {usage['prompt_tokens']}  out {usage['output_tokens']}  "
          f"cache read {usage['cache_read_tokens']}  cost ${out['cost_usd']:.4f}{' (simulated)' if args.fake else ''}  "
          f"ledger ${out['ledger_spent_usd']:.4f}/{TOTAL_LIMIT_USD:.2f}")
    print(f"-> {RESULTS / (args.tag + '.json')}")


if __name__ == "__main__":
    main()
