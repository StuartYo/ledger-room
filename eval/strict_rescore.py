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
    a = ap.parse_args(argv)
    from eval import real_photo_eval as R
    from ledger_room import pipeline, rules
    key = json.loads(Path(a.key or ROOT / "eval" / f"expected_{a.set}.json").read_text(encoding="utf-8"))
    clients, docs = pipeline.load_inbox(a.set)
    keep = {t["file"] for t in key["docs"]}
    docs = [d for d in docs if d["path"].name in keep]
    reads = pipeline.read_docs(docs, None, cache=True, model_id=a.model)
    fields = {Path(k).name: v["fields"] for k, v in reads.items()}
    res = {"set": a.set, "model": a.model, "code_rev": pipeline.git_rev(),
           "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "note": "re-scored from saved reads; no new model run", "conditions": {}}
    for name, strict in (("default", False), ("strict", True)):
        pipeline.STRICT = strict
        rdocs = [pipeline.to_rule_doc(d, reads[d["doc_id"]]["fields"], reads[d["doc_id"]]["provenance"],
                                      reads[d["doc_id"]].get("ocr_lines")) for d in docs]
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
