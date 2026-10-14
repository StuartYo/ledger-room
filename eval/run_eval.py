"""Evaluation: Ledger Room vs pure model vs controls -> eval/results.json (single source of truth).

python -m eval.run_eval              # run what is not cached yet (loads the model), then score
python -m eval.run_eval --score-only # re-score from caches only
Then: python -m eval.report          # writes eval/RESULTS.md and the README results block
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
from datetime import datetime
from pathlib import Path

from ledger_room import pipeline, rules
from ledger_room.pipeline import git_rev
from synth.generate import to_rule_doc

ROOT = Path(__file__).resolve().parents[1]
GATE = 0.95


def load_expected(set_name: str) -> dict:
    return json.loads((ROOT / "eval" / f"expected_{set_name}.json").read_text(encoding="utf-8"))


def score(pred: dict[str, set], exp: dict[str, set]) -> dict:
    out = {"per_type": {}}
    T = F = M = 0
    for t in rules.ISSUE_TYPES:
        tp = sum(1 for d in exp if t in exp[d] and t in pred.get(d, set()))
        fp = sum(1 for d in exp if t not in exp[d] and t in pred.get(d, set()))
        fn = sum(1 for d in exp if t in exp[d] and t not in pred.get(d, set()))
        T, F, M = T + tp, F + fp, M + fn
        out["per_type"][t] = _prf(tp, fp, fn)
    out["micro"] = _prf(T, F, M)
    out["docs_exact"] = sum(1 for d in exp if pred.get(d, set()) == exp[d])
    out["n_docs"] = len(exp)
    clean = [d for d in exp if not exp[d]]
    out["clean_docs"] = len(clean)
    out["clean_docs_flagged"] = sum(1 for d in clean if pred.get(d))
    return out


def _prf(tp, fp, fn) -> dict:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None
    return {"tp": tp, "fp": fp, "fn": fn, "precision": _r(p), "recall": _r(r), "f1": _r(f)}


def _r(x):
    return None if x is None else round(x, 3)


def secs(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"mean": round(statistics.mean(vals), 1), "median": round(statistics.median(vals), 1),
            "max": round(max(vals), 1), "n": len(vals)}


def oracle(set_name: str) -> dict:
    exp = load_expected(set_name)
    clients = {c["id"]: c for c in json.loads((ROOT / "data" / set_name / "clients.json").read_text(encoding="utf-8"))["clients"]}
    docs = [to_rule_doc(d["doc_id"], d["client_id"], d["month"], d["truth"]) for d in exp["docs"]]
    got = rules.issues_by_doc(rules.run_rules(docs, clients))
    return score({k: set(v) for k, v in got.items()}, {d["doc_id"]: set(d["issues"]) for d in exp["docs"]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--score-only", action="store_true")
    ap.add_argument("--sets", nargs="*", default=["main", "heldout"])
    ap.add_argument("--skip-baseline", action="store_true")
    args = ap.parse_args()

    reader = None
    if not args.score_only:
        from ledger_room.reader import Reader
        reader = Reader()
        print(f"model {reader.model_id} on {reader.device}", flush=True)

    prog = lambda tag: (lambda i, n, did, r: print(f"[{tag} {i}/{n}] {did}", flush=True))
    results = {
        "synthetic": True,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "code_rev": git_rev(),
        "reader_version": pipeline.READER_VERSION,
        "model": "Qwen3.5-2B-int4-ov (OpenVINO IR, INT4 weights)",
        "runtime": "OpenVINO GenAI 2026.4 VLMPipeline (model) + OpenVINO 2026.4 (OCR: PP-OCRv4 det/rec)",
        "hardware": "Dev laptop: Intel Core i7-13620H, Intel UHD Graphics (iGPU, device 'GPU') for the model, CPU for OCR, 32 GB RAM. "
                    "No Arc Pro B70 used; B70 figures elsewhere are plans, not measurements.",
        "host": platform.node(),
        "gate_threshold": GATE,
        "model_selection": json.loads((ROOT / "eval" / "model_selection.json").read_text(encoding="utf-8")),
        "sets": {},
    }
    for s in args.sets:
        exp = load_expected(s)
        expd = {d["doc_id"]: set(d["issues"]) for d in exp["docs"]}
        t = time.time()
        run = pipeline.audit(s, reader, run_id=f"eval-{s}", progress=prog(f"ledger {s}"))
        lr_pred = {}
        for e in run["exceptions"]:
            lr_pred.setdefault(e["doc_id"], set()).add(e["rule_id"])
        lr_time = [d["timing"].get("total_s") for d in run["docs"]]
        # rules-only time (reads cached)
        t0 = time.time()
        rdocs = [pipeline.to_rule_doc(d, d["fields"]) for d in run["docs"]]
        rules.run_rules(rdocs, {c["id"]: c for c in run["clients"]})
        rules_ms = round((time.time() - t0) * 1000, 1)
        entry = {
            "n_docs": len(expd), "n_clients": len({d['client_id'] for d in exp["docs"]}),
            "layout_family": exp["layout_family"], "seed": exp["seed"], "issue_counts": exp["issue_counts"],
            "ledger_room": {**score(lr_pred, expd), "sec_per_doc": secs(lr_time), "rules_ms_total": rules_ms,
                            "unreadable_fields": run["summary"]["unreadable_fields"]},
            "oracle_fields": oracle(s)["micro"],
        }
        if not args.skip_baseline:
            from eval.baseline import run_baseline
            base = run_baseline(s, reader, progress=prog(f"pure {s}"))
            bp = {k: set(v["issues"]) for k, v in base.items()}
            entry["pure_model"] = {**score(bp, expd), "sec_per_doc": secs([v["seconds"] for v in base.values()]),
                                   "unparsed_replies": sum(1 for v in base.values() if not v["parsed"])}
            lf, bf = entry["ledger_room"]["micro"]["f1"], entry["pure_model"]["micro"]["f1"]
            entry["pure_over_ledger_f1"] = round(bf / lf, 3) if lf and bf is not None else None
        results["sets"][s] = entry

    # positive control: before (all clean) -> after (one planted change per issue type)
    ctl = {}
    for s in ("control_before", "control_after"):
        exp = load_expected(s)
        expd = {d["doc_id"]: set(d["issues"]) for d in exp["docs"]}
        run = pipeline.audit(s, reader, run_id=f"eval-{s}", progress=prog(s))
        pred = {}
        for e in run["exceptions"]:
            pred.setdefault(e["doc_id"], set()).add(e["rule_id"])
        ctl[s] = {**score(pred, expd), "flags": sorted(f"{d}:{r}" for d, rs in pred.items() for r in rs)}
    ca = ctl["control_after"]
    results["positive_control"] = {
        "before_flags": len(ctl["control_before"]["flags"]),
        "after_planted": sum(len(v) for v in [set(d["issues"]) for d in load_expected("control_after")["docs"]]),
        "after_detected": ca["micro"]["tp"], "after_false_flags": ca["micro"]["fp"],
        "detail": ctl,
    }
    ratios = [v.get("pure_over_ledger_f1") for v in results["sets"].values() if v.get("pure_over_ledger_f1") is not None]
    results["gate"] = {"rule": f"stop if pure-model micro F1 >= {GATE:.0%} of Ledger Room on any set",
                       "max_ratio": max(ratios) if ratios else None,
                       "stop": bool(ratios) and max(ratios) >= GATE}
    (ROOT / "eval" / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: {"ledger": v["ledger_room"]["micro"], "pure": v.get("pure_model", {}).get("micro")}
                      for k, v in results["sets"].items()}, indent=1))
    print("gate:", results["gate"])


if __name__ == "__main__":
    main()
