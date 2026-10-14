"""A larger reader on the held-out set: does a bigger model alone close the gap, and how much better does it read?

python -m eval.run_larger --model-dir E:\\models\\Qwen3.5-9B-int4-ov [--device GPU] [--cpu-sample 3]

Runs, on the frozen held-out set only (no tuning, keys unchanged):
  * Ledger Room with the larger reader (same code, same rules),
  * the pure-model baseline with the larger model (same prompt as the 2B baseline),
  * reading accuracy of both readers (eval/read_accuracy.py),
  * time per document and the process's peak memory; optionally a few documents on the CPU for timing.
Writes eval/results_larger.json, which eval.run_eval merges into eval/results.json.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import time
from datetime import datetime
from pathlib import Path

from eval.read_accuracy import score_reads
from eval.run_eval import load_expected, score, secs
from ledger_room import pipeline, rules
from ledger_room.pipeline import git_rev

ROOT = Path(__file__).resolve().parents[1]


def peak_memory_gb() -> dict:
    """Peak working set and peak commit of this process (Windows). On an Intel iGPU the model's GPU buffers live in
    shared system memory, so they show up here."""
    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    try:
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
        return {"peak_working_set_gb": round(c.PeakWorkingSetSize / 2**30, 1),
                "peak_commit_gb": round(c.PeakPagefileUsage / 2**30, 1)}
    except Exception:
        return {}


def dir_gb(p: Path) -> float:
    return round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and ".cache" not in f.parts) / 2**30, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--device", default=None)
    ap.add_argument("--cpu-sample", type=int, default=0, help="also time N held-out documents on the CPU")
    a = ap.parse_args()
    from eval.baseline import run_baseline
    from ledger_room.reader import Reader

    s = "heldout"
    exp = load_expected(s)
    expd = {d["doc_id"]: set(d["issues"]) for d in exp["docs"]}
    reader = Reader(a.model_dir, a.device)
    print(f"model {reader.model_id} on {reader.device}, load {reader.load_s}s", flush=True)
    prog = lambda tag: (lambda i, n, did, r: print(f"[{tag} {i}/{n}] {did}", flush=True))   # noqa: E731
    t0 = time.time()
    run = pipeline.audit(s, reader, run_id=f"eval-{s}-{reader.model_id}", progress=prog("ledger"))
    lr_wall = time.time() - t0
    pred = {}
    for e in run["exceptions"]:
        if e["rule_id"] in rules.ISSUE_TYPES:
            pred.setdefault(e["doc_id"], set()).add(e["rule_id"])
    reads = {d["doc_id"]: d for d in run["docs"]}
    lr_times = [d["timing"].get("total_s") for d in run["docs"]]
    base = run_baseline(s, reader, progress=prog("pure"))
    bp = {k: set(v["issues"]) for k, v in base.items()}
    out = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "code_rev": git_rev(),
        "set": s, "model": reader.model_id, "device": reader.device, "load_s": reader.load_s,
        "model_on_disk_gb": dir_gb(Path(a.model_dir)),
        "hardware": "same laptop: Intel Core i7-13620H, Intel UHD iGPU (shared memory), 32 GB RAM",
        "ledger_room": {**score(pred, expd), "sec_per_doc": secs(lr_times),
                        "unreadable_fields": run["summary"]["unreadable_fields"], "review_items": run["summary"]["n_review"]},
        "pure_model": {**score(bp, expd), "sec_per_doc": secs([v["seconds"] for v in base.values()]),
                       "unparsed_replies": sum(1 for v in base.values() if not v["parsed"])},
        "reading": score_reads(reads, exp),
        "docs_per_hour": round(3600 / statistics.mean([x for x in lr_times if x])),
        "wall_s_ledger_room": round(lr_wall, 1),
        "memory": peak_memory_gb(),
    }
    if a.cpu_sample:
        del reader
        cpu = Reader(a.model_dir, "CPU")
        _, docs = pipeline.load_inbox(s)
        ts = []
        for d in docs[: a.cpu_sample]:
            t = time.time()
            cpu.read(d["path"])
            ts.append(round(time.time() - t, 1))
            print("cpu", d["doc_id"], ts[-1], flush=True)
        out["cpu_sample"] = {"docs": len(ts), "sec_per_doc": ts, "mean": round(statistics.mean(ts), 1), "load_s": cpu.load_s,
                             "memory_after": peak_memory_gb()}
    p = ROOT / "eval" / "results_larger.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("ledger_room", "pure_model")}, indent=1)[:1500])
    print("reading", out["reading"], "memory", out["memory"])


if __name__ == "__main__":
    main()
