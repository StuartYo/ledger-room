"""The review agent on the real company box (docs/real_data.md), strict reading -> eval/results_agent_real_company.json

    LEDGER_DEVICE=GPU.1 .venv/bin/python -m eval.agent_real_eval                       # official run (twice)
    .venv/bin/python -m eval.agent_real_eval --find-only                               # no model: counts held now
    .venv/bin/python -m eval.agent_real_eval --rescore [--paired FILE]                 # aggregates again from the
                                                                                       # per-voucher file, no model

Registered before the first run (2026-11-10). The first complete run is the result; the rules below are not changed
after seeing it.

Settings: the same as eval/results_agent_b70_strict.json. 9B + 27B on one card (LEDGER_DEVICE, default GPU.1), strict
reading (pipeline.STRICT), the 9B reads are the saved reads of the box (data/out/cache; no 9B re-read), the agent is
ledger_room.agent.process with all_held=True (every voucher held for a person, as eval/agent_eval.py), the 27B at
temperature 0, at most 6 tool calls, verification rule v3 (docs/agent.md). The current product rules: a recognised
utility bill goes to the e-invoice platform and is neither held nor given to the agent (commit 1d7a07c).

Definitions (answer key: eval/expected_private_mom.json, frozen, read only):
  held / sent to a person   any rule with severity hold or review on the voucher (agent.held)
  cleared                   the agent's record has status "verified": no hold or review left after the values code
                            accepted were applied and the rules ran again
  wrong (six fields)        invoice_no, date, seller_ubn, total, net, tax: a field is wrong when the key has a value
                            for it (not null, not "unreadable") and the voucher's value differs (eval/strict_rescore.py)
  wrong (three fields)      invoice_no, date, total only (the fields the company's old ledger keeps)
  value after the agent     the first reading with the accepted values applied, only when the voucher was cleared;
                            a voucher that stays held keeps its first reading (and stays with a person)
  wrong and not held        wrong and not sent to a person
  proposed value right      equal to the key; a value for a field the key has no value for ("unreadable" or null) is
                            counted apart (unscorable), never as right
  invoices_121              kinds e_invoice, three_part_invoice, two_part_register
  paired_113                the invoices paired with a row of the company's old ledger (docs/real_data.md). The list
                            of their file names is not in the repository; give it with --paired (one file name per
                            line, or a JSON list). Without it this section is null.

Privacy (docs/real_data.md): the output file holds aggregate counts only (aggregates_only: true) and is checked for
file names, invoice numbers and 8-digit numbers before it is written. The per-voucher records go to
eval/agent_real_private_mom_perdoc.json (gitignored by eval/*private_*), which stays on the host.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SET = "private_mom"
M9, M27 = "Qwen3.5-9B-int4-ov", "Qwen3.5-27B-int4-ov"
SIX = ["invoice_no", "date", "seller_ubn", "total", "net", "tax"]
THREE = ["invoice_no", "date", "total"]
INVOICE_KINDS = {"e_invoice", "three_part_invoice", "two_part_register"}
PERDOC = ROOT / "eval" / f"agent_real_{SET}_perdoc.json"
OUT = ROOT / "eval" / "results_agent_real_company.json"
REFERENCE = {
    "strict_sent_to_a_person_code_fa8c027": 257,
    "strict_sent_to_a_person_after_utility_routing": 112,
    "invoices_121_strict_sent_to_a_person": 56,
    "paired_113_strict_sent_to_a_person": 53,
    "source": "eval/results_real_company.json (first_runs, invoices_121, old_ledger_vs_ledger_room, "
              "after_fix_utility_routing)",
}


def scorable(v) -> bool:
    return v is not None and v != "unreadable"


def wrong(fields: dict, truth: dict, keys) -> list[str]:
    return [k for k in keys if scorable(truth.get(k)) and fields.get(k) != truth.get(k)]


def load_key():
    k = json.loads((ROOT / "eval" / f"expected_{SET}.json").read_text(encoding="utf-8"))
    return {t["file"]: t for t in k["docs"]}


def first_pass(reader9=None):
    from ledger_room import pipeline, rules
    clients, docs = pipeline.load_inbox(SET)
    reads = pipeline.read_docs(docs, reader9, cache=True, model_id=M9)
    rdocs = {d["doc_id"]: pipeline.to_rule_doc(d, reads[d["doc_id"]]["fields"], reads[d["doc_id"]]["provenance"],
                                               reads[d["doc_id"]].get("ocr_lines"), reads[d["doc_id"]].get("raw"))
             for d in docs}
    return clients, docs, reads, rdocs, rules.run_rules(list(rdocs.values()), clients)


def one_run(reader9, agent) -> list[dict]:
    """Per-voucher records (private: stay on the host)."""
    from ledger_room import agent as A
    key = load_key()
    clients, docs, reads, rdocs, excs = first_pass(reader9)
    held_ids = {e.doc_id for e in A.held(excs)}
    recs = A.process(docs, reads, rdocs, excs, clients, agent, cache_dir=None, all_held=True,
                     progress=lambda i, n, did, r: print(f"  agent {i}/{n}", flush=True) if i % 10 == 0 else None)
    rows = []
    for d in docs:
        f = Path(d["doc_id"]).name
        t, r = key[f], reads[d["doc_id"]]
        rec = recs.get(d["doc_id"]) if d["doc_id"] in held_ids else None
        utility = any(e.rule_id == "UTILITY_BILL_PLATFORM" for e in excs if e.doc_id == d["doc_id"])
        row = {"file": f, "kind": t.get("kind"), "utility_routed": utility, "held_before": d["doc_id"] in held_ids,
               "fields_before": {k: r["fields"].get(k) for k in SIX}}
        if rec is not None:
            cleared = rec.get("status") == "verified" and not rec.get("error")
            acc = rec.get("accepted") or {}
            row.update({
                "agent_run": True, "error": rec.get("error"), "cleared": cleared,
                "accepted": acc, "seconds": rec.get("seconds"), "steps": len(rec.get("steps") or []),
                "tools": [s.get("tool") for s in rec.get("steps") or []],
                "checks": {k: {"value": o.get("value"), "accepted": bool(o.get("accepted")), "truth": t.get(k)}
                           for k, o in (rec.get("checks") or {}).items()},
            })
            row["fields_after"] = {**row["fields_before"], **{k: v for k, v in acc.items() if k in SIX}} \
                if cleared else row["fields_before"]
        else:
            row.update({"agent_run": False, "cleared": False, "fields_after": row["fields_before"]})
        row["held_after"] = row["held_before"] and not row["cleared"]
        row["truth"] = {k: t.get(k) for k in SIX + ["buyer_ubn"]}
        rows.append(row)
    return rows


def part(rows: list[dict]) -> dict:
    """Aggregate counts for one group of vouchers."""
    def wn(keys, when):
        return sum(1 for x in rows if wrong(x["fields_" + when], x["truth"], keys) and not x["held_" + when])
    cleared = [x for x in rows if x["cleared"]]
    return {
        "vouchers": len(rows),
        "sent_to_a_person_before": sum(x["held_before"] for x in rows),
        "sent_to_a_person_after": sum(x["held_after"] for x in rows),
        "cleared": len(cleared),
        "cleared_with_a_wrong_field_six": sum(1 for x in cleared if wrong(x["fields_after"], x["truth"], SIX)),
        "cleared_with_a_wrong_field_three": sum(1 for x in cleared if wrong(x["fields_after"], x["truth"], THREE)),
        "wrong_and_not_held_before_six": wn(SIX, "before"),
        "wrong_and_not_held_after_six": wn(SIX, "after"),
        "wrong_and_not_held_before_three": wn(THREE, "before"),
        "wrong_and_not_held_after_three": wn(THREE, "after"),
    }


def aggregate(rows: list[dict], paired: set[str] | None) -> dict:
    ran = [x for x in rows if x.get("agent_run")]
    checks = [(k, o) for x in ran for k, o in x["checks"].items()]
    sc = [(k, o) for k, o in checks if scorable(o["truth"])]
    secs = [x["seconds"] for x in ran if x.get("seconds") is not None]
    not_util = [x for x in rows if not x["utility_routed"]]
    by_kind = {}
    for k in sorted({x["kind"] for x in rows}):
        g = [x for x in rows if x["kind"] == k]
        p = part(g)
        by_kind[k] = {q: p[q] for q in ("vouchers", "sent_to_a_person_before", "sent_to_a_person_after", "cleared",
                                        "cleared_with_a_wrong_field_six")}
    return {
        "whole_box": part(rows),
        "whole_box_excluding_utility_bills_routed_to_platform": {
            q: v for q, v in part(not_util).items() if q.startswith("wrong_and_not_held") or q == "vouchers"},
        "invoices_121": part([x for x in rows if x["kind"] in INVOICE_KINDS]),
        "paired_113": part([x for x in rows if x["file"] in paired]) if paired else None,
        "by_kind": by_kind,
        "agent": {
            "vouchers_given_to_the_agent": len(ran),
            "errors": sum(1 for x in ran if x.get("error")),
            "proposed_values": len(checks),
            "proposed_values_scorable": len(sc),
            "proposed_values_unscorable": len(checks) - len(sc),
            "proposed_values_wrong": sum(1 for k, o in sc if o["value"] != o["truth"]),
            "values_refused_by_code": sum(1 for k, o in checks if not o["accepted"]),
            "wrong_values_refused_by_code": sum(1 for k, o in sc if o["value"] != o["truth"] and not o["accepted"]),
            "accepted_values": sum(1 for k, o in checks if o["accepted"]),
            "accepted_values_wrong": sum(1 for k, o in sc if o["accepted"] and o["value"] != o["truth"]),
            "accepted_values_unscorable": sum(1 for k, o in checks if o["accepted"] and not scorable(o["truth"])),
            "sec_per_voucher_mean": round(sum(secs) / len(secs), 2) if secs else None,
            "sec_per_voucher_max": round(max(secs), 2) if secs else None,
            "steps_mean": round(sum(x["steps"] for x in ran) / len(ran), 2) if ran else None,
            "tool_calls": dict(Counter(t for x in ran for t in x["tools"])),
        },
    }


def outcome(rows):
    return {x["file"]: (x["cleared"], json.dumps(x.get("accepted"), sort_keys=True, default=str), tuple(x.get("tools") or ()))
            for x in rows if x.get("agent_run")}


_LEAK = [re.compile(r"[A-Z]{2}-?\d{8}"), re.compile(r"(?<!\d)\d{8}(?!\d)"),
         re.compile(r"\.(jpe?g|png)\b", re.I), re.compile(r"MOM/")]


def check_aggregate_only(doc: dict, files: list[str]) -> None:
    txt = json.dumps(doc, ensure_ascii=False)
    hits = [p.pattern for p in _LEAK if p.search(txt)] + [f for f in files if f in txt]
    if hits:
        raise SystemExit(f"refusing to write: the output would contain per-voucher data ({hits[:3]})")


def load_paired(path: str | None) -> set[str] | None:
    if not path:
        return None
    txt = Path(path).read_text(encoding="utf-8")
    try:
        v = json.loads(txt)
        return {Path(str(x)).name for x in v}
    except ValueError:
        return {Path(x.strip()).name for x in txt.splitlines() if x.strip()}


def build(runs, paired, meta) -> dict:
    agg = [aggregate(r, paired) for r in runs]
    diff = [k for k, v in outcome(runs[0]).items() if any(outcome(r).get(k) != v for r in runs[1:])]
    doc = {
        "set": "real_company (private_mom on the host)", "aggregates_only": True, "synthetic": False,
        **meta,
        "reference_before_this_run": REFERENCE,
        "result": agg[0],
        "repeats": len(runs),
        "runs_identical": not diff and all(a == agg[0] for a in agg[1:]),
        "vouchers_with_a_different_outcome_between_runs": len(diff),
        "other_runs_result": agg[1:],
        "paired_113_note": None if paired else "pairing list not given (--paired): not in the repository or on the "
                                               "host; re-aggregate with --rescore --paired FILE, no model run",
        "definitions": "eval/agent_real_eval.py docstring (registered before the first run)",
    }
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--find-only", action="store_true")
    ap.add_argument("--rescore", action="store_true", help="aggregate again from the per-voucher file, no model")
    ap.add_argument("--paired", help="file names of the 113 invoices paired with the old ledger (stays on the host)")
    ap.add_argument("--models-root", default=os.environ.get("LEDGER_MODELS_ROOT", str(Path.home() / "models")))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--notes", default=None, help="text for the notes field (e.g. what happened to an earlier run)")
    a = ap.parse_args(argv)
    from ledger_room import agent as A
    from ledger_room import pipeline
    pipeline.STRICT = True
    paired = load_paired(a.paired)
    if paired is not None:
        known = set(load_key())
        if not paired <= known:
            raise SystemExit(f"--paired: {len(paired - known)} names are not in the key")
    if a.find_only:
        _, docs, _, _, excs = first_pass(None)
        print(f"{len(docs)} vouchers, held {len({e.doc_id for e in A.held(excs)})} (strict)")
        return
    if a.rescore:
        saved = json.loads(PERDOC.read_text(encoding="utf-8"))
        doc = build(saved["runs"], paired, {**saved["meta"], "rescored_at": datetime.now().astimezone().isoformat(
            timespec="seconds"), "rescore_code_rev": pipeline.git_rev()})
        check_aggregate_only(doc, list(load_key()))
        Path(a.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        print("SUMMARY " + json.dumps(doc["result"]["whole_box"]) + " paired " + json.dumps(doc["result"]["paired_113"]))
        return
    from eval.agent_eval import Peak
    from ledger_room.reader import Reader
    device = os.environ.get("LEDGER_DEVICE", "GPU.1")
    pk = Peak()
    pk.t.start()
    mem = {"start": pk.snapshot()}
    reader9 = Reader(str(Path(a.models_root) / M9), device=device)
    mem["9B loaded"] = pk.snapshot()
    agent = A.Agent(Reader(str(Path(a.models_root) / M27), device=device))
    mem["9B+27B loaded"] = pk.snapshot()
    try:
        import openvino as ov
        dev_name = str(ov.Core().get_property(device, "FULL_DEVICE_NAME"))
    except Exception:
        dev_name = None
    print(f"9B {reader9.load_s}s, 27B {agent.load_s}s on {device} ({dev_name}), strict {pipeline.STRICT}", flush=True)
    runs = []
    t0 = time.time()
    for i in range(a.repeat):
        runs.append(one_run(reader9, agent))
        print(f"run {i + 1}: " + json.dumps(aggregate(runs[-1], paired)["whole_box"]), flush=True)
    try:   # one fresh 9B read while the 27B is loaded: both models have run on the card
        reader9.read_raw(pipeline.load_inbox(SET)[1][0]["path"])
    except Exception as e:
        mem["9B read error"] = type(e).__name__
    mem["peak"] = pk.snapshot()
    meta = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "code_rev": pipeline.git_rev(),
        "device": device, "device_name": dev_name, "measured_on_b70": bool(dev_name and "B70" in dev_name),
        "strict": True, "first_reader": M9, "first_reads": "saved reads of the box (no 9B re-read)",
        "agent_model": M27, "agent_version": A.AGENT_VERSION, "verify_version": A.VERIFY_VERSION,
        "max_steps": A.MAX_STEPS, "temperature": 0, "all_held": True,
        "utility_bills": "current product rules: routed to the e-invoice platform, not held, not given to the agent",
        "memory_gb": mem, "load_s": {"9B": reader9.load_s, "27B": agent.load_s},
        "wall_s": round(time.time() - t0, 1),
        "notes": [a.notes] if a.notes else [],
    }
    PERDOC.write_text(json.dumps({"meta": meta, "runs": runs}, ensure_ascii=False, default=str), encoding="utf-8")
    doc = build(runs, paired, meta)
    check_aggregate_only(doc, [x["file"] for x in runs[0]])
    Path(a.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print("SUMMARY " + json.dumps({"whole_box": doc["result"]["whole_box"], "identical": doc["runs_identical"],
                                   "peak": mem["peak"]}), flush=True)


if __name__ == "__main__":
    main()
