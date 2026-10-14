"""Reading accuracy: the fields Ledger Room ends up with (after code reconciliation) against the frozen answer key.

Reading error rate = wrong key fields / key fields compared, over: document type, invoice number, date, seller UBN,
buyer UBN, net, tax, total. A field is compared when the key or the read has a value. Item lines are scored
separately: a document's items count as right when every item in the key is found (same text after removing spaces)
among the items read. Art. 19 depends on item text, so this is the number a stronger reader has to move.
"""
from __future__ import annotations

import re

KEY_FIELDS = ["doc_type", "invoice_no", "date", "seller_ubn", "buyer_ubn", "net", "tax", "total"]


def _norm(s) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def score_reads(reads: dict[str, dict], expected: dict) -> dict:
    per = {f: {"wrong": 0, "n": 0} for f in KEY_FIELDS}
    items_ok = docs_ok = n = 0
    for d in expected["docs"]:
        r = reads.get(d["doc_id"])
        if r is None:
            continue
        n += 1
        got, want = r["fields"], d["truth"]
        doc_ok = True
        for f in KEY_FIELDS:
            a, b = got.get(f), want.get(f)
            if a is None and b is None:
                continue
            per[f]["n"] += 1
            if a != b:
                per[f]["wrong"] += 1
                doc_ok = False
        read_items = {_norm(i.get("desc")) for i in got.get("items") or []}
        if all(_norm(i["desc"]) in read_items for i in want.get("items") or []):
            items_ok += 1
        docs_ok += doc_ok
    wrong = sum(v["wrong"] for v in per.values())
    total = sum(v["n"] for v in per.values())
    return {"docs": n, "field_error_rate": round(wrong / total, 3) if total else None,
            "fields_wrong": wrong, "fields_compared": total,
            "docs_all_key_fields_right": docs_ok, "docs_items_right": items_ok,
            "per_field": {f: f"{v['wrong']}/{v['n']}" for f, v in per.items()}}
