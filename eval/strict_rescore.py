"""Re-score a real-photo set from saved reads, default vs strict reading (docs/strict_draft.md). No model is loaded:
every read must already be in data/out/cache (from an official run).

    python -I eval/strict_rescore.py real_tw --model Qwen3.5-9B-int4-ov --out eval/results_real_tw_strict.json

On real_tw the strict rule was written after seeing that set's misses: label the output "after the fix", never a
first run."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("set")
    ap.add_argument("--model", required=True)
    ap.add_argument("--key", help="answer key (default eval/expected_<set>.json)")
    ap.add_argument("--out")
    ap.add_argument("--model-dir", help="load the reader: saved model replies are reused, the model only runs what is "
                                        "not saved (the UBN zoom vote, replies from an older prompt)")
    ap.add_argument("--device", default="GPU")
    ap.add_argument("--second-model-dir", help="strict + second reader (27B): escalated photos are re-read on the "
                                               "same OCR lines and merged (ledger_room/tiered.py)")
    ap.add_argument("--second-device", default="GPU.1")
    a = ap.parse_args(argv)
    from eval import real_photo_eval as R
    from ledger_room import pipeline, rules
    key = json.loads(Path(a.key or ROOT / "eval" / f"expected_{a.set}.json").read_text(encoding="utf-8"))
    clients, docs = pipeline.load_inbox(a.set)
    keep = {t["file"] for t in key["docs"]}
    docs = [d for d in docs if d["path"].name in keep]
    reader = None
    if a.model_dir:
        from ledger_room.reader import Reader
        reader = Reader(a.model_dir, a.device)
    reads = pipeline.read_docs(docs, reader, cache=True, model_id=a.model)
    fields = {Path(k).name: v["fields"] for k, v in reads.items()}
    res = {"set": a.set, "model": a.model, "code_rev": pipeline.git_rev(),
           "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "note": "re-scored from saved reads" + ("; the reader re-ran only what was not saved" if a.model_dir else "; no model run"), "conditions": {}}
    conds = [("default", False, reads), ("strict", True, reads)]
    if a.second_model_dir:
        import time
        from ledger_room import tiered
        from ledger_room.reader import Reader
        pipeline.STRICT = True
        second = Reader(a.second_model_dir, a.second_device)
        merged, esc, secs = dict(reads), {}, []
        for d in docs:
            first = reads[d["doc_id"]]
            why = tiered.escalation_reasons(first)
            if not why:
                continue
            cp = pipeline.OUT / "cache" / f"{pipeline.cache_key(pipeline.sha256_file(d['path']), second.model_id, raw=True)}_ocrreuse.json"
            raw2 = json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else None
            t0 = time.time()
            raw2, r2, changed = tiered.read_with_ocr(second, d["path"], first["ocr_lines"], raw2)
            secs.append(round(time.time() - t0, 2))
            if changed:
                cp.write_text(json.dumps(raw2, ensure_ascii=False), encoding="utf-8")
            merged[d["doc_id"]] = tiered.merge(first, r2, why)
            esc[Path(d["doc_id"]).name] = {"reasons": why, "taken": merged[d["doc_id"]]["tier"]["taken_from_second"]}
        res["second_reader"] = {"model": second.model_id, "device": second.device, "escalated": len(esc),
                                "sec_per_escalated_photo": round(sum(secs) / len(secs), 2) if secs else None,
                                "per_photo": esc}
        conds.append(("strict_with_second_reader", True, merged))
    for name, strict, rd in conds:
        pipeline.STRICT = strict
        fields = {Path(k).name: v["fields"] for k, v in rd.items()}
        rdocs = [pipeline.to_rule_doc(d, rd[d["doc_id"]]["fields"], rd[d["doc_id"]]["provenance"],
                                      rd[d["doc_id"]].get("ocr_lines"), rd[d["doc_id"]].get("raw")) for d in docs]
        ex = rules.run_rules(rdocs, clients)
        held = {Path(e.doc_id).name for e in ex if e.severity in ("hold", "review")}
        sc = R.score_tw(key["docs"], fields, held)
        wrong = set()
        for t in key["docs"]:
            g = fields.get(t["file"]) or {}
            if any(t.get(f) not in (None, "unreadable") and g.get(f) != t.get(f) for f in R.TW_FIELDS):
                wrong.add(t["file"])
        sc["wrong_and_not_held_files"] = sorted(wrong - held)
        sc["hold_rules"] = dict(Counter(e.rule_id for e in ex if e.severity in ("hold", "review")))
        res["conditions"][name] = sc
    pipeline.STRICT = False
    txt = json.dumps(res, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
