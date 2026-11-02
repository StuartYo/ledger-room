"""Total reading: before / after the amount reconciliation, next to the pure model, on one set.

python -m eval.totals_eval --set main --model-dir E:\\models\\Qwen3.5-2B-int4-ov [--device GPU]
python -m eval.totals_eval --set private_real --truth TRUTH.json --model-dir ... [--device CPU]   (aggregates only)
python -m eval.totals_eval --set main --score-only        (cached reads and answers only, no model)

Columns, all from the same model on the same photo and OCR lines:
  before      Ledger Room's amounts with the reader at commit 862a875 (the code before this change), re-applied to the
              same cached model reply. Only the amount post-processing differs; every other field is the current read.
  after       Ledger Room now: the amount reconciliation in reader.py (labelled rows, e-invoice header slot,
              net + tax = total triples, amount in words, payment rows dropped, a follow-up total question) and the
              READ_REVIEW hold for a total that two readings do not agree on.
  model text  the model's own net / tax / total in its transcription reply, no code at all.
  pure model  the pure-model auditor (eval/baseline.py): same model, rules in the prompt, it reports the total itself.

Writes eval/results_totals.json under "<set>|<model>" (merged; other entries kept).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from ledger_room import normalize as N
from ledger_room import pipeline, rules
from ledger_room.ocr import Line

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval" / "results_totals.json"
BEFORE_REV = "862a875"   # heldout2 frozen; reader.py unchanged since badd55d


def old_postprocess():
    """reader.postprocess as it was before this change, loaded from git (amounts only are used)."""
    src = subprocess.run(["git", "show", f"{BEFORE_REV}:ledger_room/reader.py"], cwd=ROOT, capture_output=True,
                         check=True).stdout.decode("utf-8")
    spec = importlib.util.spec_from_loader("ledger_room._reader_before", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "ledger_room"
    sys.modules[spec.name] = mod
    exec(compile(src, f"{BEFORE_REV}:ledger_room/reader.py", "exec"), mod.__dict__)
    return mod.postprocess


def held_ids(exceptions: list[dict]) -> set[str]:
    return {e["doc_id"] for e in exceptions if e["severity"] in ("hold", "review")}


def run_rules(set_name: str, docs_fields: dict[str, tuple[dict, dict, list]]) -> list[dict]:
    clients, docs = pipeline.load_inbox(set_name)
    rdocs = [pipeline.to_rule_doc(d, *docs_fields[d["doc_id"]]) for d in docs]
    return [e.to_dict() for e in rules.run_rules(rdocs, clients)]


def _cmp(got, want) -> int | None:
    """1 right, 0 wrong, None when neither has a value (not compared)."""
    if got is None and want is None:
        return None
    return int(got == want)


def _frac(xs) -> str:
    xs = [x for x in xs if x is not None]
    return f"{sum(xs)}/{len(xs)}"


def score_synthetic(set_name, exp, after, before, model_text, pure, ex_after, ex_before) -> dict:
    out = {"docs": len(exp["docs"])}
    held_a, held_b = held_ids(ex_after), held_ids(ex_before)
    near = {d["doc_id"]: d.get("printed_near_total") for d in exp["docs"]}

    def amounts(fields_by_id, held):
        per = {f: [] for f in ("net", "tax", "total")}
        amt_bad = silent = false_hold = 0
        took_other = 0
        for d in exp["docs"]:
            got, want = fields_by_id[d["doc_id"]], d["truth"]
            for f in per:
                per[f].append(_cmp(got.get(f), want.get(f)))
            bad = any(want.get(f) is not None and got.get(f) != want.get(f) for f in per) \
                or (want.get("tax") is None and bool(got.get("tax")))
            amt_bad += bad
            if held is not None:
                silent += bad and d["doc_id"] not in held
            p = near.get(d["doc_id"]) or {}
            others = {v for k, v in (p.get("payment") or {}).items() if isinstance(v, int) and k != "paid"} \
                | ({p["payment"]["paid"]} if p.get("payment") and p["payment"]["paid"] != want["total"] else set()) \
                | ({p["deposit"], want["total"] - p["deposit"]} if p.get("deposit") else set())
            took_other += got.get("total") is not None and got.get("total") != want["total"] and got.get("total") in others
        r = {f: _frac(v) for f, v in per.items()}
        r["docs_with_amount_error"] = amt_bad
        if held is not None:
            r["amount_errors_not_held"] = silent
        if any(near.values()):
            r["total_is_cash_change_balance_or_deposit"] = took_other
        return r

    out["after"] = amounts(after, held_a)
    out["before"] = amounts(before, held_b)
    out["model_text"] = amounts(model_text, None)
    if pure is not None:
        out["pure_model"] = {"total": _frac(_cmp(pure[d["doc_id"]].get("total"), d["truth"]["total"]) for d in exp["docs"]),
                             "total_wrong_all_unflagged": sum(1 for d in exp["docs"]
                                                              if pure[d["doc_id"]].get("total") != d["truth"]["total"])}
        if any(near.values()):
            out["pure_model"]["total_is_cash_change_balance_or_deposit"] = amounts(
                {k: {"total": v.get("total")} for k, v in pure.items()}, None).get("total_is_cash_change_balance_or_deposit")

    # decisions: held documents, held with right amounts, OVER_LIMIT, the six-issue micro F1
    from eval.run_eval import score
    expd = {d["doc_id"]: set(d["issues"]) for d in exp["docs"]}
    for name, ex, fields in (("after", ex_after, after), ("before", ex_before, before)):
        pred = rules.issues_by_doc([_exc(e) for e in ex])
        s = score({k: set(v) for k, v in pred.items()}, expd)
        right = {d["doc_id"] for d in exp["docs"] if all(fields[d["doc_id"]].get(f) == d["truth"].get(f)
                                                         for f in ("net", "tax", "total"))}
        amt_rules = {e["doc_id"] for e in ex if e["rule_id"] == "AMOUNT_MISMATCH"
                     or (e["rule_id"] == "READ_REVIEW" and "total" in e["evidence_fields"])}
        out[name].update({
            "docs_held_for_a_person": len(held_ids(ex)),
            "held_by_amount_checks_with_right_amounts": len(amt_rules & right),
            "over_limit": {k: s["per_type"]["OVER_LIMIT"][k] for k in ("tp", "fp", "fn")},
            "micro_f1": s["micro"]["f1"], "clean_docs_flagged": s["clean_docs_flagged"],
        })
    if pure is not None:
        bp = {k: set(v["issues"]) for k, v in pure.items()}
        s = score(bp, expd)
        out["pure_model"].update({"over_limit": {k: s["per_type"]["OVER_LIMIT"][k] for k in ("tp", "fp", "fn")},
                                  "micro_f1": s["micro"]["f1"]})
    return out


def _exc(e: dict) -> rules.Exception_:
    return rules.Exception_(e["doc_id"], e["client_id"], e["rule_id"], e["severity"], e.get("message", ""),
                            e.get("evidence_fields", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True)
    ap.add_argument("--model-dir")
    ap.add_argument("--model-id", default="Qwen3.5-2B-int4-ov", help="with --score-only")
    ap.add_argument("--device")
    ap.add_argument("--truth", help="real photos: hand-written truth (kept outside the repository)")
    ap.add_argument("--score-only", action="store_true")
    ap.add_argument("--skip-baseline", action="store_true")
    ap.add_argument("--label", default=None, help="extra note stored with the entry (e.g. 'first and only run')")
    ap.add_argument("--freeze", action="store_true",
                    help="mark the entry as the first and only run of a held-out set: later runs refuse to overwrite it")
    a = ap.parse_args()
    reader = None
    if a.score_only:
        mid = a.model_id
    else:
        from ledger_room.reader import Reader
        reader = Reader(a.model_dir, a.device) if a.model_dir else Reader(device=a.device)
        mid = reader.model_id
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    if prev.get(f"{a.set}|{mid}", {}).get("frozen"):
        raise SystemExit(f"{a.set}|{mid} is frozen (first and only run recorded); not running it again")
    run = pipeline.audit(a.set, reader, run_id=f"totals-{a.set}-{mid}", model_id=mid,
                         progress=lambda i, n, did, r: print(f"[{i}/{n}] {did}", flush=True))
    after = {d["doc_id"]: d["fields"] for d in run["docs"]}
    prov_after = {d["doc_id"]: d["provenance"] for d in run["docs"]}

    # before: the old amount post-processing on the same cached model reply; every other field as read now
    old_pp = old_postprocess()
    _, docs = pipeline.load_inbox(a.set)
    reads = pipeline.read_docs(docs, None, model_id=mid)   # all cached by the audit above
    cdir = pipeline.OUT / "cache"
    before, model_text, fields_before = {}, {}, {}
    for d in docs:
        r = reads[d["doc_id"]]
        raw = json.loads((cdir / f"{pipeline.cache_key(pipeline.sha256_file(d['path']), mid, raw=True)}.json")
                         .read_text(encoding="utf-8"))
        lines = [Line(x["id"], x["text"], tuple(x["box"]), x["conf"]) for x in raw["ocr_lines"]]
        obj = N.parse_json_loose(raw["raw"]) or {}
        of, op, _, _ = old_pp(obj, lines)
        f = dict(r["fields"])
        f.update(net=of["net"], tax=of["tax"], total=of["total"])
        p = {k: v for k, v in r["provenance"].items() if k not in ("total", "net", "tax")}
        p["total"] = op.get("total")   # the old reader never marked a total unconfirmed
        before[d["doc_id"]] = f
        fields_before[d["doc_id"]] = (f, p, r.get("ocr_lines"))
        model_text[d["doc_id"]] = {"net": N.to_int(obj.get("net")), "tax": N.to_int(obj.get("tax")),
                                   "total": N.to_int(obj.get("total")) or None}
    ex_after = run["exceptions"]
    ex_before = run_rules(a.set, fields_before)
    pure = None
    if not a.skip_baseline:
        from eval.baseline import run_baseline
        pure = run_baseline(a.set, reader, model_id=mid,
                            progress=lambda i, n, did, r: print(f"[pure {i}/{n}] {did}", flush=True))

    prov_counts = Counter(p.get("total") for p in prov_after.values())
    asked = sum(1 for d in run["docs"] if d["timing"].get("ask_s"))
    entry = {"set": a.set, "model": mid, "device": reader.device if reader else "cache", "code_rev": pipeline.git_rev(),
             "reader_version": pipeline.READER_VERSION, "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "total_provenance": dict(prov_counts), "follow_up_total_questions": asked,
             "mean_read_s": run["summary"]["mean_read_s"]}
    if a.label:
        entry["note"] = a.label
    if a.freeze:
        entry["frozen"] = True
    if a.truth:   # real photos: totals only, aggregates only
        T = json.loads(Path(a.truth).read_text(encoding="utf-8"))
        by = {Path(k).name: k for k in after}
        held_a, held_b = held_ids(ex_after), held_ids(ex_before)
        pf = {Path(k).name: v for k, v in (pure or {}).items()}
        rows = [t for t in T["docs"] if t.get("total") is not None]

        def tally(fields, held):
            right = sum(1 for t in rows if fields[by[t["file"]]].get("total") == t["total"])
            silent = None if held is None else sum(1 for t in rows if fields[by[t["file"]]].get("total") != t["total"]
                                                   and by[t["file"]] not in held)
            return right, silent
        ra, sa = tally(after, held_a)
        rb, sb = tally(before, held_b)
        rm, _ = tally(model_text, None)
        entry.update({
            "blind": False, "note_blind": "10 real receipts already seen during development; not a blind test",
            "totals_in_key": len(rows),
            "after": {"total": f"{ra}/{len(rows)}", "total_wrong_not_held": sa,
                      "captures_held_for_a_person": f"{len(held_a)}/{len(after)}"},
            "before": {"total": f"{rb}/{len(rows)}", "total_wrong_not_held": sb,
                       "captures_held_for_a_person": f"{len(held_b)}/{len(after)}"},
            "model_text": {"total": f"{rm}/{len(rows)}"},
        })
        if pure is not None:
            entry["pure_model"] = {"total": f"{sum(1 for t in rows if pf[t['file']].get('total') == t['total'])}/{len(rows)}",
                                   "total_wrong_all_unflagged": sum(1 for t in rows if pf[t["file"]].get("total") != t["total"])}
    else:
        exp = json.loads((ROOT / "eval" / f"expected_{a.set}.json").read_text(encoding="utf-8"))
        entry.update(score_synthetic(a.set, exp, after, before, model_text, pure, ex_after, ex_before))
    allr = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    key = f"{a.set}|{mid}"
    if key in allr and allr[key].get("frozen"):
        raise SystemExit(f"{key} is frozen (first and only run recorded); refusing to overwrite")
    allr[key] = entry
    OUT.write_text(json.dumps(allr, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(entry, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
