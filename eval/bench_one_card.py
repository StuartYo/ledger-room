"""Both readers on ONE B70: the 9B and the 27B second reader loaded on the same card, in one process -> eval/results_one_card.json

    .venv/bin/python -m eval.bench_one_card --set heldout --n 20 --device GPU.0

Answers one question a judge asked: "a 9B fits any 8 GB card, why a 32 GB B70?" The Pro set-up keeps the 9B
(reads every document) and the 27B (re-reads the ones the 9B is unsure about, ledger_room/tiered.py) loaded at the
same time. Earlier each was measured alone (eval/results_b70.json: 7.99 GB and 17.04 GB peak card memory).
This run loads both on one card, reads N documents fresh (cache off) with the 9B, re-reads EVERY one of them with
the 27B (worst case for memory: both models busy, both KV caches filled), and records:

  peak card memory of this process (Linux DRM fdinfo, same method as eval/bench_b70.py PeakMemory),
  seconds a document for each reader, and whether any read failed (e.g. out of card memory).

It scores nothing and changes no product code. If the load or a read fails, that failure is the result.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def card_mem_gb() -> dict:
    tot, seen = {}, set()
    for f in Path(f"/proc/{os.getpid()}/fdinfo").glob("*"):
        try:
            txt = f.read_text()
        except Exception:
            continue
        if "drm-driver" not in txt:
            continue
        cid = re.search(r"drm-client-id:\s*(\d+)", txt)
        if cid and cid.group(1) in seen:
            continue
        if cid:
            seen.add(cid.group(1))
        for m in re.finditer(r"^(drm-(?:total|resident)-(?:vram\d*|gtt|system\d*|memory)):\s*(\d+)\s*(KiB|MiB|B)?",
                             txt, flags=re.M):
            mult = {"KiB": 1 / 2**20, "MiB": 1 / 2**10, "B": 1 / 2**30, None: 1 / 2**20}[m.group(3)]
            k = "gpu_" + m.group(1).replace("drm-", "").replace("-", "_") + "_gb"
            tot[k] = tot.get(k, 0.0) + int(m.group(2)) * mult
    return tot


class Peak:
    def __init__(self):
        self.peak, self.stop = {}, threading.Event()
        self.t = threading.Thread(target=self.run, daemon=True)

    def sample(self):
        for k, v in card_mem_gb().items():
            self.peak[k] = max(self.peak.get(k, 0.0), v)

    def run(self):
        while not self.stop.wait(0.5):
            self.sample()

    def snapshot(self):
        self.sample()
        return {k: round(v, 2) for k, v in self.peak.items()}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="heldout")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--device", default="GPU.0")
    ap.add_argument("--models-root", default=str(Path.home() / "models"))
    ap.add_argument("--out", default=str(ROOT / "eval" / "results_one_card.json"))
    a = ap.parse_args(argv)
    from ledger_room import pipeline, tiered
    from ledger_room.reader import Reader

    res = {"what": __doc__.split("It scores")[0].strip(), "set": a.set, "device": a.device, "code_rev": pipeline.git_rev(),
           "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "ok": False, "stages": {}}
    pk = Peak()
    pk.t.start()
    try:
        _, docs = pipeline.load_inbox(a.set)
        docs = docs[: a.n]
        t = time.time()
        r9 = Reader(str(Path(a.models_root) / "Qwen3.5-9B-int4-ov"), a.device)
        res["stages"]["9B loaded"] = {"s": round(time.time() - t, 1), "mem": pk.snapshot()}
        t = time.time()
        r27 = Reader(str(Path(a.models_root) / "Qwen3.5-27B-int4-ov"), a.device)
        res["stages"]["27B loaded too"] = {"s": round(time.time() - t, 1), "mem": pk.snapshot()}
        t = time.time()
        reads = pipeline.read_docs(docs, r9, cache=False)
        s9 = time.time() - t
        res["stages"]["9B read all"] = {"docs": len(docs), "sec_per_doc": round(s9 / len(docs), 2), "mem": pk.snapshot()}
        t, fails = time.time(), 0
        for d in docs:
            try:
                tiered.read_with_ocr(r27, d["path"], reads[d["doc_id"]]["ocr_lines"], None)
            except Exception:
                fails += 1
        s27 = time.time() - t
        res["stages"]["27B re-read all"] = {"docs": len(docs), "failed": fails, "sec_per_doc": round(s27 / len(docs), 2),
                                            "mem": pk.snapshot()}
        res["ok"] = fails == 0
    except Exception as e:
        res["error"] = "".join(traceback.format_exception_only(type(e), e)).strip()[:500]
    pk.stop.set()
    res["peak_card_memory"] = pk.snapshot()
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("ok", "peak_card_memory", "stages")} | {"error": res.get("error")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
