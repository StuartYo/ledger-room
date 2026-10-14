"""Dev tool: field-level read accuracy on the MAIN set only (never used on held-out for tuning).

python -m eval.read_check --n 12
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from ledger_room.reader import Reader

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ["doc_type", "invoice_no", "date", "invoice_period", "seller_ubn", "buyer_ubn", "total", "note"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--ids", nargs="*")
    args = ap.parse_args()
    exp = json.loads((ROOT / "eval" / "expected_main.json").read_text(encoding="utf-8"))
    docs = exp["docs"]
    if args.ids:
        docs = [d for d in docs if d["doc_id"] in args.ids]
    else:
        docs = random.Random(args.seed).sample(docs, min(args.n, len(docs)))
    r = Reader()
    print("model", r.model_id, "device", r.device, "load", r.load_s)
    ok = {f: 0 for f in FIELDS}
    times = []
    for d in docs:
        t = time.time()
        res = r.read(ROOT / "data" / "main" / "clients" / d["doc_id"])
        times.append(time.time() - t)
        f, tr = res["fields"], d["truth"]
        bad = []
        for k in FIELDS:
            a, b = f.get(k), tr.get(k)
            if k == "note":
                good = (a is None) == (b is None)
            else:
                good = a == b
            ok[k] += good
            if not good:
                bad.append(f"{k}: got {a!r} want {b!r} [{res['provenance'].get(k, '')}]")
        print(f"{d['doc_id']} {tr['doc_type'][:4]} {times[-1]:.1f}s ttft={res['timing']['ttft_s']} tok={res['timing']['gen_tokens']} "
              f"{'OK' if not bad else ''}")
        for b in bad:
            print("    ", b)
        if not res["parsed"]:
            print("     RAW:", res["raw"][:300])
    n = len(docs)
    print("accuracy:", {k: f"{v}/{n}" for k, v in ok.items()})
    print(f"mean {sum(times) / n:.1f}s/doc")


if __name__ == "__main__":
    main()
