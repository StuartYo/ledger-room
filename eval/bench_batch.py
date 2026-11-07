"""One 9B, many vouchers at once: does batching on one B70 read faster, with the same answers? -> eval/results_batch.json

    .venv/bin/python -m eval.bench_batch --set heldout --device GPU.0 --batches 1,2,4,8,16

Why: one voucher takes about 3.3 s of model time on the B70, and about 2.8 s of that is writing the answer token by
token (host cache, 1,879 reads: ttft 0.51 s, vlm 3.33 s, 214 tokens). Writing one token for one voucher reads all the
weights once; a batch writes one token for each of N vouchers in the same pass. Running 1 to 4 separate reader
processes on one card did not help (eval/results_concurrency.json): they took turns. This tries batching inside one
reader instead, with OpenVINO GenAI's ContinuousBatchingPipeline (images supported since 2026.2).

The product reader (ledger_room/reader.py, VLMPipeline, one at a time) is NOT changed. For each document this script
builds exactly the prompt and image the product sends (same OCR, same EXTRACT_PROMPT, same thinking-off wrap, same
resize, greedy decoding, same max tokens), then:

  reference  the product's VLMPipeline, one document at a time (model time only, OCR excluded)
  batch B    ContinuousBatchingPipeline, documents sent B at a time (B = 1, 2, 4, ...), model time only

and records, per B: documents an hour (model time), the share of replies that are byte-identical to the reference,
and the share of documents whose post-processed fields (reader.postprocess, the code that decides the fields) are
identical to the reference. OCR runs on the CPU and is timed separately; it does not change with B.
A batch whose fields differ from the reference on any document is reported as such: its accuracy would have to be
measured again before anyone could use it. The answer key is not used.
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="heldout")
    ap.add_argument("--n", type=int, default=None, help="first N documents (default: all)")
    ap.add_argument("--device", default="GPU.0")
    ap.add_argument("--model-dir", default=str(Path.home() / "models" / "Qwen3.5-9B-int4-ov"))
    ap.add_argument("--batches", default="1,2,4,8,16")
    ap.add_argument("--cache-gb", type=int, default=8, help="KV cache for the batch pipeline (SchedulerConfig.cache_size)")
    ap.add_argument("--out", default=str(ROOT / "eval" / "results_batch.json"))
    a = ap.parse_args(argv)
    import openvino as ov
    import openvino_genai as g
    from ledger_room import normalize as N
    from ledger_room import pipeline
    from ledger_room import reader as R

    res = {"what": __doc__.split("The answer key")[0].strip(), "set": a.set, "device": a.device, "model": Path(a.model_dir).name,
           "code_rev": pipeline.git_rev(), "openvino_genai": getattr(g, "__version__", None),
           "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "cache_gb": a.cache_gb, "runs": {}}

    def save():
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    _, docs = pipeline.load_inbox(a.set)
    docs = docs[: a.n] if a.n else docs
    res["docs"] = len(docs)

    # same inputs as Reader.read_raw / Reader.generate
    t = time.time()
    items = []
    for d in docs:
        img = Image.open(d["path"]).convert("RGB")
        lines = R.read_lines(d["path"])
        im = img
        s = R.VLM_MAX_SIDE / max(im.size)
        if s < 1:
            im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
        items.append({"doc": d["doc_id"], "lines": lines, "tensor": ov.Tensor(np.array(im)[None]),
                      "prompt": R.EXTRACT_PROMPT.format(ocr=R._fmt_ocr(lines), cats=", ".join(R.CATEGORIES))})
    res["ocr_s_per_doc"] = round((time.time() - t) / len(docs), 3)

    def fields_of(text, lines):
        obj = N.parse_json_loose(text) or {}
        f, p, _, _ = R.postprocess(obj, lines)
        return json.dumps([f, p], ensure_ascii=False, sort_keys=True, default=str)

    # reference: the product's reader, one at a time
    ref = R.Reader(a.model_dir, a.device)
    wrap = ref.wrap
    ref.generate(items[0]["prompt"], Image.fromarray(items[0]["tensor"].data[0]))      # warm-up, not timed
    t = time.time()
    ref_txt = []
    for it in items:
        txt, _ = ref.generate(it["prompt"], Image.fromarray(it["tensor"].data[0]))
        ref_txt.append(txt)
    ref_s = time.time() - t
    ref_f = [fields_of(x, it["lines"]) for x, it in zip(ref_txt, items)]
    res["reference"] = {"how": "VLMPipeline (the product reader), one at a time", "model_s_per_doc": round(ref_s / len(items), 3),
                        "docs_per_hour_model_time": round(3600 * len(items) / ref_s)}
    save()
    del ref
    gc.collect()

    try:
        sc = g.SchedulerConfig()
        sc.cache_size = a.cache_gb
        sc.max_num_seqs = max(int(b) for b in a.batches.split(","))
        sc.enable_prefix_caching = False
        t = time.time()
        cb = g.ContinuousBatchingPipeline(a.model_dir, sc, a.device)
        res["batch_pipeline_load_s"] = round(time.time() - t, 1)
    except Exception as e:
        res["batch_pipeline_error"] = "".join(traceback.format_exception_only(type(e), e)).strip()[:800]
        save()
        print(json.dumps(res, ensure_ascii=False)[:2000])
        return

    def cfg():
        c = g.GenerationConfig()
        c.max_new_tokens = 360
        c.do_sample = False
        if wrap:
            c.apply_chat_template = False
        return c

    prompts = [R.WRAP.format(prompt=it["prompt"]) if wrap else it["prompt"] for it in items]
    cb.generate(prompts[:1], [[items[0]["tensor"]]], [cfg()])                             # warm-up, not timed
    for b in [int(x) for x in a.batches.split(",")]:
        run = {"batch": b}
        try:
            t = time.time()
            out = []
            for i in range(0, len(items), b):
                r = cb.generate(prompts[i:i + b], [[it["tensor"]] for it in items[i:i + b]], [cfg() for _ in items[i:i + b]])
                out += [x.texts[0] for x in r]
            s = time.time() - t
            same_txt = sum(x == y for x, y in zip(out, ref_txt))
            same_f = sum(fields_of(x, it["lines"]) == y for x, it, y in zip(out, items, ref_f))
            run.update(model_s_per_doc=round(s / len(items), 3), docs_per_hour_model_time=round(3600 * len(items) / s),
                       replies_identical=f"{same_txt}/{len(items)}", fields_identical=f"{same_f}/{len(items)}",
                       fields_differ_docs=[it["doc"] for x, it, y in zip(out, items, ref_f) if fields_of(x, it["lines"]) != y][:20])
        except Exception as e:
            run["error"] = "".join(traceback.format_exception_only(type(e), e)).strip()[:800]
        res["runs"][str(b)] = run
        save()
        print(json.dumps(run, ensure_ascii=False))
    print(json.dumps({k: res.get(k) for k in ("reference", "ocr_s_per_doc", "batch_pipeline_load_s")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
