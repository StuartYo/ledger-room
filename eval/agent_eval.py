"""The review agent (ledger_room/agent.py) on the held documents of the synthetic sets -> eval/results_agent_b70.json

    LEDGER_DEVICE=GPU.1 .venv/bin/python -m eval.agent_eval              # 9B + 27B on one card, all four sets, twice
    .venv/bin/python -m eval.agent_eval --find-only                      # no model: which documents are held

What it measures (the same as the 2026-11-09 experiment, now through the product module):
  first pass   the current reading (9B, cached reads) and the rules -> documents held for a person (hold / review).
               Rules as the experiment ran them: not strict, no platform download, no business register.
  agent        ledger_room.agent.process on every held document (27B, temperature 0, at most 6 tool calls).
  score        a document is "cleared" when no hold or review is left after the code-checked fixes; it is compared
               with the frozen answer key (eval/expected_<set>.json, read only) on eight key fields. A cleared
               document with any key field wrong "slipped through".
  control      the plain code rule without a model: when the only hold is "total not confirmed" and net and tax were
               confirmed on the page, total := net + tax.

targets_binding is true only for a full run on an Arc Pro B70 (the bench convention, eval/bench_b70.py).
Experiment numbers this should reproduce: held 30, cleared 10, slipped 0 (hard 18 -> 4, blind3 12 -> 6).
Synthetic data only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ledger_room import agent as A  # noqa: E402
from ledger_room import pipeline, rules  # noqa: E402

SETS = ["hard", "blind3", "heldout2", "heldout"]
M9, M27 = "Qwen3.5-9B-int4-ov", "Qwen3.5-27B-int4-ov"
KEY = A.KEY_FIELDS
EXPECT = {"held": 30, "cleared": 10, "slipped": 0, "by_set": {"hard": [18, 4], "blind3": [12, 6]}}


def truth_map(set_name):
    e = json.loads((ROOT / "eval" / f"expected_{set_name}.json").read_text(encoding="utf-8"))
    return {d["doc_id"]: d["truth"] for d in e["docs"]}


def wrong_fields(fields, truth):
    return [k for k in KEY if not (fields.get(k) is None and truth.get(k) is None) and fields.get(k) != truth.get(k)]


def first_pass(set_name, reader9):
    clients, docs = pipeline.load_inbox(set_name)
    reads = pipeline.read_docs(docs, reader9, cache=True, model_id=M9)
    rdocs = {d["doc_id"]: pipeline.to_rule_doc(d, reads[d["doc_id"]]["fields"], reads[d["doc_id"]]["provenance"],
                                               reads[d["doc_id"]].get("ocr_lines"), reads[d["doc_id"]].get("raw"))
             for d in docs}
    return clients, docs, reads, rdocs, rules.run_rules(list(rdocs.values()), clients)


def code_rule_only(rec, read, truth) -> str | None:
    """None: the plain rule does not clear it; else "right" / "wrong"."""
    if [f"{e['rule_id']}:{','.join(e['evidence_fields'])}" for e in rec["held_for"]] != ["READ_REVIEW:total"]:
        return None
    f, p = read["fields"], read["provenance"]
    net, tax = f.get("net"), f.get("tax")
    if p.get("net") in A.WEAK or p.get("tax") in A.WEAK or not net or not tax or abs(tax - net * 0.05) > 1:
        return None
    ok = net + tax == truth.get("total") and not [k for k in KEY if k != "total" and f.get(k) != truth.get(k)]
    return "right" if ok else "wrong"


class Peak:
    def __init__(self):
        self.peak, self.stop = {}, threading.Event()
        self.t = threading.Thread(target=self.run, daemon=True)

    def sample(self):
        for k, v in A.card_memory_gb().items():
            self.peak[k] = max(self.peak.get(k, 0.0), v)

    def run(self):
        while not self.stop.wait(0.5):
            self.sample()

    def snapshot(self):
        self.sample()
        return {k: round(v, 2) for k, v in self.peak.items()}


def one_run(sets, reader9, agent, ocr=None) -> dict:
    out = {"sets": {}, "docs": []}
    for s in sets:
        clients, docs, reads, rdocs, excs = first_pass(s, reader9)
        truth = truth_map(s)
        held_ids = sorted({e.doc_id for e in A.held(excs)})
        base_slip = [d["doc_id"] for d in docs if d["doc_id"] not in held_ids
                     and wrong_fields(reads[d["doc_id"]]["fields"], truth[d["doc_id"]])]
        t0 = time.time()
        recs = A.process(docs, reads, rdocs, excs, clients, agent, cache_dir=None, all_held=True, ocr=ocr) \
            if agent is not None else {}
        for doc_id in held_ids:
            r, rec, tr = reads[doc_id], recs.get(doc_id), truth[doc_id]
            if rec is None:
                out["docs"].append({"set": s, "doc_id": doc_id, "error": "agent not run"})
                continue
            newf = {**r["fields"], **rec["accepted"]}
            cleared = rec["status"] == "verified"
            out["docs"].append({
                "set": s, "doc_id": doc_id, "held_for": [f"{e['rule_id']}:{','.join(e['evidence_fields'])}"
                                                         for e in rec["held_for"]],
                "first_wrong": wrong_fields(r["fields"], tr), "tools": [x.get("tool") for x in rec["steps"]],
                "proposed": rec["proposed"], "need_human": rec["need_human"],
                "checks": {k: {**{q: o.get(q) for q in ("value", "accepted", "evidence", "why")},
                               "truth": tr.get(k), "right": o.get("value") == tr.get(k)}
                           for k, o in rec["checks"].items()},
                "accepted": rec["accepted"], "still_held_for": rec["still_held_for"], "cleared": cleared,
                "final_wrong": wrong_fields(newf, tr) if cleared else None,
                "accepted_wrong": [k for k, v in rec["accepted"].items() if v != tr.get(k)],
                "code_rule_only": code_rule_only(rec, r, tr), "seconds": rec["seconds"],
                "steps": rec["steps"], "error": rec.get("error"),
            })
            if rec.get("error"):
                out["docs"][-1]["cleared"] = False
        rows = [x for x in out["docs"] if x["set"] == s and not x.get("error")]
        out["sets"][s] = {"docs": len(docs), "held": len(held_ids), "baseline_slipped": len(base_slip),
                          "cleared": sum(x["cleared"] for x in rows),
                          "cleared_wrong": sum(1 for x in rows if x["cleared"] and x["final_wrong"]),
                          "agent_seconds": round(time.time() - t0, 1)}
        print(f"{s}: {len(docs)} docs, held {len(held_ids)}, cleared {out['sets'][s]['cleared']}, "
              f"wrong {out['sets'][s]['cleared_wrong']}", flush=True)
    rows = [x for x in out["docs"] if not x.get("error")]
    cleared = [x for x in rows if x["cleared"]]
    rule_c = [x for x in rows if x["code_rule_only"]]
    out["summary"] = {
        "held": len(out["docs"]), "errors": len(out["docs"]) - len(rows), "cleared": len(cleared),
        "slipped": sum(1 for x in cleared if x["final_wrong"]),
        "proposed_values": sum(len(x["checks"]) for x in rows),
        "proposed_values_wrong": sum(1 for x in rows for o in x["checks"].values() if not o["right"]),
        "wrong_values_refused_by_code": sum(1 for x in rows for o in x["checks"].values()
                                            if not o["right"] and not o["accepted"]),
        "accepted_values_wrong": sum(len(x["accepted_wrong"]) for x in rows),
        "cleared_by_arithmetic_only": sum(1 for x in cleared if all(
            set(o.get("evidence") or []) <= {"arithmetic"} for o in x["checks"].values() if o["accepted"])),
        "code_rule_only": {"cleared": len(rule_c), "wrong": sum(x["code_rule_only"] == "wrong" for x in rule_c)},
        "sec_per_doc_mean": round(sum(x["seconds"] or 0 for x in rows) / len(rows), 2) if rows else None,
        "sec_per_doc_max": max((x["seconds"] or 0 for x in rows), default=None),
        "steps_mean": round(sum(len(x["steps"]) for x in rows) / len(rows), 2) if rows else None,
    }
    return out


def outcome(run) -> dict:
    return {x["doc_id"]: (x.get("cleared"), json.dumps(x.get("accepted"), sort_keys=True), tuple(x.get("tools") or ()))
            for x in run["docs"]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", default=",".join(SETS))
    ap.add_argument("--repeat", type=int, default=2, help="runs of the agent (greedy: they should be identical)")
    ap.add_argument("--find-only", action="store_true", help="no model: list the held documents from cached reads")
    ap.add_argument("--models-root", default=os.environ.get("LEDGER_MODELS_ROOT", str(Path.home() / "models")))
    ap.add_argument("--out", default=str(ROOT / "eval" / "results_agent_b70.json"))
    a = ap.parse_args(argv)
    pipeline.STRICT = False      # as the experiment and the other evaluations score the reading
    sets = a.sets.split(",")
    if a.find_only:
        for s in sets:
            _, docs, _, _, excs = first_pass(s, None)
            ids = sorted({e.doc_id for e in A.held(excs)})
            print(s, len(docs), "held", len(ids))
            for i in ids:
                print("  ", i, [f"{e.rule_id}:{','.join(e.evidence_fields)}" for e in A.held(excs, i)])
        return
    device = os.environ.get("LEDGER_DEVICE", "GPU.1")
    from ledger_room.reader import Reader
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
    on_b70 = bool(dev_name and "B70" in dev_name)
    print(f"9B {reader9.load_s}s, 27B {agent.load_s}s on {device} ({dev_name})", flush=True)
    runs = []
    for i in range(a.repeat):
        runs.append(one_run(sets, reader9, agent))
        print(f"run {i + 1}: " + json.dumps(runs[-1]["summary"]), flush=True)
    try:   # one fresh 9B read while the 27B is loaded: both models have run on the card
        reader9.read_raw(pipeline.load_inbox(sets[0])[1][0]["path"])
    except Exception as e:
        mem["9B read error"] = str(e)[:200]
    mem["peak"] = pk.snapshot()
    s0 = runs[0]["summary"]
    by = runs[0]["sets"]
    reproduced = (s0["held"] == EXPECT["held"] and s0["cleared"] == EXPECT["cleared"] and s0["slipped"] == 0
                  and all([by.get(k, {}).get("held"), by.get(k, {}).get("cleared")] == v
                          for k, v in EXPECT["by_set"].items()))
    diff = [k for k, v in outcome(runs[0]).items() if any(outcome(r).get(k) != v for r in runs[1:])]
    doc = {
        "synthetic": True, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "code_rev": pipeline.git_rev(), "device": device, "device_name": dev_name, "measured_on_b70": on_b70,
        "targets_binding": on_b70 and sets == SETS, "strict": False,
        "first_reader": M9, "agent_model": M27, "agent_version": A.AGENT_VERSION, "verify_version": A.VERIFY_VERSION,
        "max_steps": A.MAX_STEPS, "temperature": 0,
        "experiment_numbers": EXPECT, "reproduced": reproduced,
        "repeats": len(runs), "docs_with_different_outcome_between_runs": diff,
        "summary": s0, "sets": by, "memory_gb": mem,
        "load_s": {"9B": reader9.load_s, "27B": agent.load_s},
        "docs": runs[0]["docs"], "other_runs_summary": [r["summary"] for r in runs[1:]],
        "notes": ["The verification rule (v3) was tuned on the hard set during the experiment; blind3, heldout2 and "
                  "heldout were first run after it was fixed.",
                  "The history and platform tools were never used: the synthetic sets rarely repeat a seller and the "
                  "hard and blind3 sets have no platform download.",
                  "Synthetic data only; no real company documents."],
    }
    Path(a.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("SUMMARY " + json.dumps({"reproduced": reproduced, "binding": doc["targets_binding"], **s0,
                                   "differ": len(diff), "peak": mem["peak"]}), flush=True)


if __name__ == "__main__":
    main()
