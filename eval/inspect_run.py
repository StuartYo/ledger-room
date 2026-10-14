"""Dev tool: score one pipeline run against its answer key and list the misses (main set only for tuning).

python -m eval.inspect_run data/out/runs/<run_id>/result.json
"""
import json
import sys
from pathlib import Path

from eval.run_eval import load_expected, score

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ["doc_type", "invoice_no", "date", "invoice_period", "seller_ubn", "buyer_ubn", "total"]

res = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
exp = load_expected(res["summary"]["set"])
expd = {d["doc_id"]: set(d["issues"]) for d in exp["docs"]}
truth = {d["doc_id"]: d["truth"] for d in exp["docs"]}
pred = {}
from ledger_room.rules import ISSUE_TYPES
reviews = [e for e in res["exceptions"] if e["rule_id"] == "READ_REVIEW"]
for e in res["exceptions"]:
    if e["rule_id"] in ISSUE_TYPES:
        pred.setdefault(e["doc_id"], set()).add(e["rule_id"])
print("review items:", [(e["doc_id"], e["message"]) for e in reviews])
sc = score(pred, expd)
print("micro", sc["micro"], "exact", sc["docs_exact"], "/", sc["n_docs"], "clean flagged", sc["clean_docs_flagged"])
for t, v in sc["per_type"].items():
    print(f"  {t:20s} {v}")
docs = {d["doc_id"]: d for d in res["docs"]}
for did in sorted(expd):
    p, e = pred.get(did, set()), expd[did]
    if p != e:
        print(f"\n{did}: want {sorted(e)} got {sorted(p)}")
        f = docs[did]["fields"]
        for k in FIELDS:
            if f.get(k) != truth[did].get(k):
                print(f"   {k}: read {f.get(k)!r} truth {truth[did].get(k)!r} [{docs[did]['provenance'].get(k)}]")
        if f.get("note") != truth[did].get("note"):
            print(f"   note: read {f.get('note')!r} truth {truth[did].get('note')!r}")
        print("   items:", [i["desc"] for i in f.get("items", [])], "| truth:", [i["desc"] for i in truth[did]["items"]],
              "| category:", f.get("category"))
