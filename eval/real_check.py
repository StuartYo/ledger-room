"""Real-photo check against a ground truth written by hand before any model saw the photos.

python -m eval.real_check --set private_real --truth TRUTH.json --model-dir DIR [--device GPU|CPU] --out PRIVATE.json
    [--aggregate EVAL.json] [--skip-baseline]

The photos and truth stay outside the repository (data/private_* is gitignored). --aggregate writes counts and rates
only (no invoice numbers, sellers, amounts or file names) for eval/results.json.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

from ledger_room import pipeline, rules

FIELDS = ["doc_type", "invoice_no", "date", "seller_ubn", "total"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--device", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--aggregate")
    ap.add_argument("--skip-baseline", action="store_true")
    ap.add_argument("--score-only", action="store_true")
    a = ap.parse_args()
    T = json.loads(Path(a.truth).read_text(encoding="utf-8"))
    mid = Path(a.model_dir).name
    reader = None
    if not a.score_only:
        from ledger_room.reader import Reader
        reader = Reader(a.model_dir, a.device)
    run = pipeline.audit(a.set, reader, run_id=f"real-{mid}", model_id=mid,
                         progress=lambda i, n, did, r: print(f"[{i}/{n}] {did}", flush=True))
    by_file = {Path(d["doc_id"]).name: d for d in run["docs"]}
    ex_by_file: dict[str, list] = {}
    for e in run["exceptions"]:
        ex_by_file.setdefault(Path(e["doc_id"]).name, []).append(e)
    # field accuracy on the unique receipts
    per = {f: [0, 0] for f in FIELDS}
    for t in T["docs"]:
        got = by_file[t["file"]]["fields"]
        for f in FIELDS:
            want = t.get(f)
            if want is None and f != "invoice_no" and f != "seller_ubn":
                continue
            per[f][1] += 1
            per[f][0] += (got.get(f) == want)
    # decisions
    D = T["expected_decisions"]
    export = {Path(r["doc_id"]).name: r for r in __import__("ledger_room.export", fromlist=["rows"]).rows(run)}
    unique = [t["file"] for t in T["docs"]]
    ready = sum(1 for f in unique if export[f]["status"] == "READY")
    nav_hit = sum(1 for f in D["not_a_voucher"] if any(e["rule_id"] == "NOT_A_VOUCHER" for e in ex_by_file.get(f, [])))
    nav_false = sum(1 for f in unique if f not in D["not_a_voucher"]
                    and any(e["rule_id"] == "NOT_A_VOUCHER" for e in ex_by_file.get(f, [])))
    is_dup = lambda f: any(e["rule_id"].startswith("DUP_") for e in ex_by_file.get(f, []))   # noqa: E731
    def linked(a_, b_):   # a duplicate finding on one of the pair that points at the other
        return any(e["rule_id"].startswith("DUP_") and any(Path(r).name == y for r in e.get("related_docs") or [])
                   for x, y in ((a_, b_), (b_, a_)) for e in ex_by_file.get(x, []))
    pair_hit = sum(1 for a_, b_ in D["possible_duplicate_pairs"] if linked(a_, b_))
    rep_hit = sum(1 for a_, b_ in D["duplicate_pairs_same_invoice"] if linked(a_, b_))
    dup_files = {f for pair in D["possible_duplicate_pairs"] + D["duplicate_pairs_same_invoice"] for f in pair}
    dup_false = sum(1 for f in by_file if f not in dup_files and is_dup(f))
    ubn_false = sum(1 for e in run["exceptions"] if e["rule_id"] == "UBN_CHECKSUM")
    times = [d["timing"].get("total_s") for d in run["docs"] if d["timing"].get("total_s")]
    res = {
        "model": mid, "device": reader.device if reader else "cache", "photos": len({t["photo"] for t in T["docs"]}),
        "receipts": len(T["docs"]), "repeat_captures": len(T["repeat_captures"]),
        "kinds": dict(Counter(t["kind"] for t in T["docs"])),
        "field_accuracy": {f: f"{v[0]}/{v[1]}" for f, v in per.items()},
        "fields_right": f"{sum(v[0] for v in per.values())}/{sum(v[1] for v in per.values())}",
        "lines_ready_to_deduct": f"{ready}/{len(unique)} (expected 0: no voucher names the client)",
        "not_a_voucher_held": f"{nav_hit}/{len(D['not_a_voucher'])}", "not_a_voucher_false": nav_false,
        "detail_and_payment_slip_flagged_as_possible_duplicate": f"{pair_hit}/{len(D['possible_duplicate_pairs'])}",
        "repeat_captures_flagged_as_duplicate": f"{rep_hit}/{len(D['duplicate_pairs_same_invoice'])}",
        "other_documents_flagged_as_duplicate": dup_false,
        "ubn_checksum_false_alarms": ubn_false,
        "sec_per_doc": round(statistics.mean(times), 1) if times else None,
        "exceptions_by_rule": dict(Counter(e["rule_id"] for e in run["exceptions"])),
    }
    if not a.skip_baseline:
        from eval.baseline import run_baseline
        base = run_baseline(a.set, reader, model_id=mid)
        bf = {Path(k).name: v for k, v in base.items()}
        pf = {"invoice_no": "no", "date": "date", "seller_ubn": "seller_ubn", "total": "total"}
        pacc = {}
        for f, k in pf.items():
            n = ok = 0
            for t in T["docs"]:
                want = t.get(f)
                if want is None and f not in ("invoice_no", "seller_ubn"):
                    continue
                got = bf[t["file"]].get(k)
                if f == "invoice_no":
                    got = rules.norm_invoice_no(got)
                n += 1
                ok += got == want
            pacc[f] = f"{ok}/{n}"
        res["pure_model"] = {
            "field_accuracy": pacc,
            "lines_marked_with_no_issue": f"{sum(1 for t in T['docs'] if not bf[t['file']]['issues'])}/{len(T['docs'])}",
            "documents_flagged": f"{sum(1 for v in bf.values() if v['issues'])}/{len(bf)}",
            "repeat_captures_flagged_as_duplicate": f"{sum(1 for a_, b_ in D['duplicate_pairs_same_invoice'] if any(x.startswith('DUP_') for x in bf[a_]['issues'] + bf[b_]['issues']))}/{len(D['duplicate_pairs_same_invoice'])}",
            "detail_and_payment_slip_flagged": f"{sum(1 for a_, b_ in D['possible_duplicate_pairs'] if any(x.startswith('DUP_') for x in bf[a_]['issues'] + bf[b_]['issues']))}/{len(D['possible_duplicate_pairs'])}",
            "ubn_checksum_false_alarms": sum(1 for v in bf.values() if "UBN_CHECKSUM" in v["issues"]),
            "issues_by_type": dict(Counter(x for v in bf.values() for x in v["issues"])),
        }
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    if a.aggregate:
        p = Path(a.aggregate)
        allr = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        allr[mid] = res
        p.write_text(json.dumps(allr, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
