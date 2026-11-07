"""Reproduce the B70 CL_OUT_OF_RESOURCES reader error and test a padding work-around.

The 70-minute long run (results_longrun.json) restarted a reader 28 times. Every failing document was a landscape
photo that the reader shrinks to 896 x 645..673; no portrait photo and no landscape photo shrunk to 674+ px high
failed. This script reads only those images, N times each, in one of two modes:
  orig  the reader's own preprocessing (shrink to 896 on the long side)
  pad   the same shrink, then white padding at the bottom to at least PAD_H px high (default 704)
A reader error ends the child process (the GPU plugin is unusable afterwards) and a fresh one continues.

Host:
  .venv/bin/python -m eval.repro_oor --mode orig --device GPU.0 --reps 10 --out ~/oor_orig.json
  .venv/bin/python -m eval.repro_oor --mode pad  --device GPU.1 --reps 10 --out ~/oor_pad.json
  .venv/bin/python -m eval.repro_oor --mode pipeline --device GPU.0 --reps 8 --out ~/oor_pipeline.json
pipeline: the full read of each document (OCR + every model call, incl. the UBN zoom crop), recording the prompt and
image size of every model call, so an error can be tied to the call that raised it.

Result on the B70, 2026-11-07: orig 320 reads / 0 errors, pad 320 / 0 — the plain page read alone does not fail.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETS = ["heldout", "hard", "blind3", "main"]


def failing_images(lo=645, hi=673, side=896):
    from PIL import Image
    out = []
    for s in SETS:
        for p in sorted((ROOT / "data" / s).rglob("*.jpg")):
            w, h = Image.open(p).size
            if w <= h:
                continue
            k = side / max(w, h)
            H = round(h * k) if k < 1 else h
            if lo <= H <= hi:
                out.append(str(p))
    return out


def prep(path, mode, pad_h):
    from PIL import Image
    from ledger_room.reader import VLM_MAX_SIDE
    im = Image.open(path).convert("RGB")
    s = VLM_MAX_SIDE / max(im.size)
    if s < 1:
        im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    if mode == "pad" and im.height < pad_h:
        c = Image.new("RGB", (im.width, pad_h), (255, 255, 255))
        c.paste(im, (0, 0))
        im = c
    return im


def child(model_dir, device, mode, pad_h, conn):
    import ledger_room  # noqa: F401
    from ledger_room.reader import Reader, EXTRACT_PROMPT
    r = Reader(model_dir, device)
    calls = []          # pipeline mode: every generate call of the current document (prompt head, image size)
    if mode == "pipeline":
        from ledger_room import pipeline
        pipeline.OUT = Path(os.path.expanduser("~/oor_pipeline_out"))
        orig_gen = r.generate

        def gen(prompt, image, *a, **k):
            calls.append({"prompt": prompt[:30], "size": list(image.size)})
            return orig_gen(prompt, image, *a, **k)
        r.generate = gen
    conn.send(("ready", r.load_s))
    while True:
        job = conn.recv()
        if job is None:
            return
        t = time.time()
        calls.clear()
        try:
            if mode == "pipeline":
                d = {"doc_id": os.path.relpath(job, ROOT), "path": Path(job), "client": "x"}
                pipeline.read_docs([d], r, cache=False)
            else:
                r.generate(EXTRACT_PROMPT, prep(job, mode, pad_h))
            conn.send(("ok", job, {"s": round(time.time() - t, 2), "calls": list(calls)}))
        except Exception as ex:  # noqa: BLE001
            conn.send(("error", job, {"detail": f"{type(ex).__name__}: {ex}"[:200], "calls": list(calls)}))
            return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["orig", "pad", "pipeline"], required=True)
    ap.add_argument("--device", default="GPU.0")
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--pad-h", type=int, default=704)
    ap.add_argument("--model", default=os.path.expanduser("~/models/Qwen3.5-9B-int4-ov"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    imgs = failing_images()
    queue = [p for _ in range(a.reps) for p in imgs]
    ctx = mp.get_context("spawn")
    ok = errors = restarts = 0
    err_docs, lat, seen = [], [], {}
    t0 = time.time()
    while queue:
        pc, cc = ctx.Pipe()
        proc = ctx.Process(target=child, args=(a.model, a.device, a.mode, a.pad_h, cc), daemon=True)
        proc.start()
        msg = pc.recv()
        while queue:
            pc.send(queue[0])
            if not pc.poll(180):
                kind, job = "error", queue[0]
                info = "timeout"
            else:
                m = pc.recv()
                kind, job, info = m
            queue.pop(0)
            if kind == "ok":
                ok += 1
                lat.append(info["s"])
                for c in info["calls"]:
                    key = f'{c["prompt"][:12]}|{c["size"][0]}x{c["size"][1]}'
                    seen[key] = seen.get(key, 0) + 1
            else:
                errors += 1
                err_docs.append({"doc": os.path.relpath(job, ROOT), **(info if isinstance(info, dict) else {"detail": info})})
                break
        try:
            pc.send(None)
        except Exception:  # noqa: BLE001
            pass
        proc.join(30)
        if proc.is_alive():
            proc.kill()
        if queue:
            restarts += 1
        print(f"[{a.mode}] ok {ok} errors {errors} left {len(queue)}", flush=True)
    res = {"mode": a.mode, "pad_h": a.pad_h if a.mode == "pad" else None, "device": a.device,
           "model": Path(a.model).name, "images": len(imgs), "reps": a.reps, "reads": ok + errors, "ok": ok,
           "errors": errors, "restarts": restarts, "error_rate": round(errors / max(1, ok + errors), 4),
           "mean_latency_s": round(sum(lat) / max(1, len(lat)), 2), "wall_min": round((time.time() - t0) / 60, 1),
           "error_docs": err_docs, "ok_call_shapes": dict(sorted(seen.items(), key=lambda x: -x[1])[:200]),
           "measured_on_b70": True}
    Path(os.path.expanduser(a.out)).write_text(json.dumps(res, indent=1))
    print(json.dumps({k: v for k, v in res.items() if k != "error_docs"}))


if __name__ == "__main__":
    main()
