"""Hard set: the same damaged synthetic vouchers read by OCR only and by OCR + the vision model.

python -m eval.hard_eval --reader ocr
python -m eval.hard_eval --reader model --model-dir E:\\models\\Qwen3.5-9B-int4-ov --device GPU
python -m eval.hard_eval --reader model --model-dir E:\\models\\Qwen3.5-2B-int4-ov --device GPU --limit 4 --dry-run

Writes eval/results_hard.json (one entry per reader; a later run of the same reader replaces only its own entry).
All documents are synthetic and clean (no planted issue); the answer key (eval/expected_hard.json, from
synth/hard.py) says for every key field whether a person can still read it on the image.

Per damage type it reports:
  * key fields read wrong, counted only over fields a person can read;
  * documents held for a person (any hold or review routing flag, as in eval/run_eval.py);
  * wrong value and not held: a readable key field was read as a different value and nothing stopped the document.
    This is the number that matters: a misread that can go straight into the media filing file. Listed document by
    document. "wrong and not held" also counts fields that were not read at all (missing);
  * expected hold, not held: a key field cannot be read on the image, yet the document was not sent to a person;
  * false alarms: any of the six issue types raised here (every voucher is clean, so each one comes from a misread).
--limit / --dry-run run a few documents (one per damage type first) to check that the script works; the entry is
marked dry_run and its numbers are not results.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SET = "hard"
KEY_FIELDS = ["doc_type", "invoice_no", "date", "seller_ubn", "buyer_ubn", "net", "tax", "total"]
HOLD_SEVERITIES = ("hold", "review")
OUT_FILE = ROOT / "eval" / "results_hard.json"


def load_expected() -> dict:
    return json.loads((ROOT / "eval" / "expected_hard.json").read_text(encoding="utf-8"))


def pick_subset(exp: dict, n: int) -> list[str]:
    """n documents, one per damage type first (in the key's order), so a short dry run touches every type."""
    by_type: dict[str, list[str]] = {}
    for d in exp["docs"]:
        by_type.setdefault(d["degradation"]["type"], []).append(d["doc_id"])
    out, k = [], 0
    while len(out) < n and any(k < len(v) for v in by_type.values()):
        for t in exp["damage_types"]:
            if k < len(by_type.get(t, [])) and len(out) < n:
                out.append(by_type[t][k])
        k += 1
    return out


def subset_inbox(doc_ids: list[str], tag: str) -> str:
    """Copy the chosen documents into data/out/hard_subset_<tag> (gitignored). data/hard is never touched."""
    from ledger_room import pipeline
    name = f"out/hard_subset_{tag}"
    base = pipeline.DATA / name
    if base.exists():
        shutil.rmtree(base)
    (base / "clients").mkdir(parents=True)
    shutil.copy2(pipeline.DATA / SET / "clients.json", base / "clients.json")
    for did in doc_ids:
        dst = base / "clients" / did
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pipeline.DATA / SET / "clients" / did, dst)
    return name


def judge(exp: dict, run: dict, issue_types) -> dict:
    reads = {d["doc_id"]: d for d in run["docs"]}
    held = {e["doc_id"] for e in run["exceptions"] if e["severity"] in HOLD_SEVERITIES}
    flags: dict[str, list[str]] = {}
    for e in run["exceptions"]:
        flags.setdefault(e["doc_id"], []).append(e["rule_id"])
    per_doc = []
    for d in exp["docs"]:
        r = reads.get(d["doc_id"])
        if r is None:
            continue
        got, want, readable = r["fields"], d["truth"], d["human_readable"]
        wrong, n_cmp, unreadable = [], 0, {}
        for f in KEY_FIELDS:
            a, b = got.get(f), want.get(f)
            if readable.get(f, True):
                if a is None and b is None:
                    continue
                n_cmp += 1
                if a != b:
                    wrong.append({"field": f, "read": a, "truth": b, "kind": "missing" if a is None else "wrong_value"})
            else:
                unreadable[f] = {"read": a, "read_right_anyway": a == b}
        is_held = d["doc_id"] in held
        false_alarms = sorted({x for x in flags.get(d["doc_id"], []) if x in issue_types})
        per_doc.append({
            "doc_id": d["doc_id"], "type": d["degradation"]["type"], "variant": d["degradation"]["variant"],
            "expect_hold": d["expect_hold"], "held": is_held, "fields_compared": n_cmp, "wrong": wrong,
            "unreadable_fields": unreadable, "wrong_not_held": bool(wrong) and not is_held,
            "wrong_value_not_held": any(w["kind"] == "wrong_value" for w in wrong) and not is_held,
            "expect_hold_not_held": d["expect_hold"] and not is_held, "false_alarms": false_alarms,
            "routing_flags": sorted({x for x in flags.get(d["doc_id"], []) if x not in issue_types}),
            "seconds": (r.get("timing") or {}).get("total_s"),
        })

    def agg(rows: list[dict]) -> dict:
        fc = sum(x["fields_compared"] for x in rows)
        fw = sum(len(x["wrong"]) for x in rows)
        secs = [x["seconds"] for x in rows if x["seconds"] is not None]
        return {
            "docs": len(rows), "fields_compared": fc, "fields_wrong": fw,
            "field_error_rate": round(fw / fc, 3) if fc else None,
            "docs_held": sum(x["held"] for x in rows),
            "docs_expect_hold": sum(x["expect_hold"] for x in rows),
            "expect_hold_and_held": sum(x["expect_hold"] and x["held"] for x in rows),
            "expect_hold_not_held": sum(x["expect_hold_not_held"] for x in rows),
            "held_although_readable": sum(x["held"] and not x["expect_hold"] for x in rows),
            "wrong_not_held": sum(x["wrong_not_held"] for x in rows),
            "wrong_not_held_docs": [x["doc_id"] for x in rows if x["wrong_not_held"]],
            "wrong_value_not_held": sum(x["wrong_value_not_held"] for x in rows),
            "wrong_value_not_held_docs": [x["doc_id"] for x in rows if x["wrong_value_not_held"]],
            "fields_missing": sum(1 for x in rows for w in x["wrong"] if w["kind"] == "missing"),
            "false_alarms": sum(len(x["false_alarms"]) for x in rows),
            "sec_per_doc_mean": round(statistics.mean(secs), 2) if secs else None,
        }

    types = exp["damage_types"]
    return {"overall": agg(per_doc),
            "per_type": {t: agg([x for x in per_doc if x["type"] == t]) for t in types
                         if any(x["type"] == t for x in per_doc)},
            "per_doc": per_doc}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reader", choices=["ocr", "model"], required=True)
    ap.add_argument("--model-dir", type=Path, help="vision-language model folder (OpenVINO IR), --reader model")
    ap.add_argument("--device", default="GPU", help="OpenVINO device for the model (GPU, CPU)")
    ap.add_argument("--limit", type=int, help="only this many documents (one per damage type first)")
    ap.add_argument("--dry-run", action="store_true", help="mark the entry as a dry run (not a result)")
    ap.add_argument("--no-cache", action="store_true", help="read fresh instead of using data/out/cache")
    a = ap.parse_args(argv)

    from ledger_room import pipeline, rules
    exp = load_expected()
    if a.reader == "ocr":
        from eval.ocr_only import OcrOnlyReader
        reader = OcrOnlyReader()
        name, model_id = "ocr-only", "ocr-only"
    else:
        if not a.model_dir:
            ap.error("--reader model needs --model-dir")
        from ledger_room.reader import Reader
        reader = Reader(str(a.model_dir), a.device)
        model_id = a.model_dir.name
        name = f"{model_id} + OCR"
    tag = ("ocr" if a.reader == "ocr" else model_id).replace(".", "_")
    ids = pick_subset(exp, a.limit) if a.limit else [d["doc_id"] for d in exp["docs"]]
    set_name = subset_inbox(ids, tag) if a.limit else SET
    exp_run = {**exp, "docs": [d for d in exp["docs"] if d["doc_id"] in set(ids)]}
    run = pipeline.audit(set_name, reader, run_id=f"hard-{tag}-{datetime.now():%Y%m%d-%H%M%S}",
                         cache=not a.no_cache, model_id=model_id,
                         progress=lambda i, n, did, r: print(f"[hard {name} {i}/{n}] {did}", flush=True))
    res = judge(exp_run, run, rules.ISSUE_TYPES)
    dry = bool(a.dry_run or a.limit)
    entry = {
        "reader": name, "model": model_id, "device": getattr(reader, "device", None),
        "dry_run": dry, "note": "dry run to check the script: not a result" if dry else None,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "code_rev": pipeline.git_rev(),
        "host": __import__("platform").node(), "n_docs": len(exp_run["docs"]), "run_id": run["summary"]["run_id"],
        **res,
    }
    out = json.loads(OUT_FILE.read_text(encoding="utf-8")) if OUT_FILE.exists() else {}
    out.update({"synthetic": True, "set": SET,
                "about": "Damaged synthetic vouchers (synth/hard.py); every voucher is clean. Fields a person cannot "
                         "read are not scored as wrong: the expected outcome there is a hold."})
    key = name + (" (dry run)" if dry else "")
    out.setdefault("readers", {})[key] = entry
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    o = res["overall"]
    print(json.dumps({"reader": key, "field_error_rate": o["field_error_rate"], "held": o["docs_held"],
                      "expect_hold_not_held": o["expect_hold_not_held"], "wrong_not_held": o["wrong_not_held"],
                      "wrong_value_not_held": o["wrong_value_not_held"],
                      "false_alarms": o["false_alarms"]}, ensure_ascii=False))
    print(json.dumps({t: (v["field_error_rate"], v["docs_held"], v["wrong_not_held"])
                      for t, v in res["per_type"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
