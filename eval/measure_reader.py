"""Peak memory and read time of one reader on a few held-out documents (fresh reads; nothing is cached or scored).

python -m eval.measure_reader --model-dir E:\\models\\Qwen3.5-2B-int4-ov --device GPU --n 3
Merges {model: {...}} into eval/reader_memory.json, which eval.run_eval copies into results.json.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from eval.run_larger import dir_gb, peak_memory_gb
from ledger_room import pipeline

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--device", default=None)
    ap.add_argument("--n", type=int, default=3)
    a = ap.parse_args()
    from ledger_room.reader import Reader
    r = Reader(a.model_dir, a.device)
    _, docs = pipeline.load_inbox("heldout")
    ts = []
    for d in docs[: a.n]:
        t = time.time()
        r.read(d["path"])
        ts.append(round(time.time() - t, 1))
    out = {"model": r.model_id, "device": r.device, "docs": len(ts), "sec_per_doc": ts,
           "disk_gb": dir_gb(Path(a.model_dir)), "load_s": r.load_s, **peak_memory_gb(),
           "measured_on": time.strftime("%Y-%m-%d")}
    p = ROOT / "eval" / "reader_memory.json"
    allm = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    allm[r.model_id] = out
    p.write_text(json.dumps(allm, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
