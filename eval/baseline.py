"""Pure-model baseline: the SAME local model on the SAME inputs (photo + OCR lines),
asked to audit each document directly with the rules written into the prompt.

No code checks. The only help it gets: for duplicate detection, a running list of
the documents it already reviewed, built from ITS OWN previous answers (code only
concatenates text; it never compares anything).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from PIL import Image

from ledger_room import normalize as N
from ledger_room.ocr import read_lines
from ledger_room.pipeline import OUT, load_inbox, sha256_file
from ledger_room.rules import ISSUE_TYPES

BASELINE_PROMPT = """You are an auditor at a Taiwanese bookkeeping firm. Audit ONE document submitted by the client below. You have no tools; decide everything yourself.

Client: {client_name} (統一編號 {client_ubn}). Document folder: {month}. Filing period: 2026-09-01 to 2026-10-31 (ROC 115, Sep-Oct).
Client expense policy, per document: meal <= NT${meal}; taxi <= NT${taxi}; lodging <= NT${lodging}; any document above NT${any} needs approval.

Rules. Report every rule id that applies:
UBN_CHECKSUM: a 統一編號 (seller or buyer) printed on the document fails the checksum. Algorithm: weights 1,2,1,2,1,2,4,1; multiply each digit by its weight; add up the digits of every product (e.g. 14 -> 1+4); the number is valid if the sum is divisible by 5; if the 7th digit is 7 it is also valid when sum+1 is divisible by 5 (2023 rule).
DUP_CROSS_MONTH: this client already submitted the same document earlier (same invoice number, or same seller UBN + date + total) - check the list of earlier documents.
DUP_CROSS_CLIENT: the same document appears under a DIFFERENT client in the list of earlier documents.
S19_NONDEDUCTIBLE: an invoice issued to this client's 統一編號 with input tax (三聯式 invoice, or e-invoice showing 買方 = client) for entertainment or gifts, employee personal benefits, a passenger car for own use, or non-business items (VAT Act Art. 19).
OVER_LIMIT: the total exceeds the client policy above.
PERIOD_MISMATCH: the document date is outside the filing period, or an e-invoice period header (e.g. 115年09-10月) does not contain the printed date.

Earlier documents already reviewed (client | folder | invoice no | seller UBN | date | total), from your own earlier answers:
{memory}

OCR text lines of this document (digits are reliable):
{ocr}

Reply with ONE line of JSON only: {{"no":"invoice number or null","seller_ubn":"8 digits","date":"YYYY-MM-DD","total":0,"issues":["RULE_ID"]}}"""


def run_baseline(set_name: str, reader, *, cache: bool = True, progress=None,
                 model_id: str = "Qwen3.5-2B-int4-ov") -> dict:
    clients, docs = load_inbox(set_name)
    docs = sorted(docs, key=lambda d: (d["month"], d["client_id"], d["doc_id"]))
    cpath = OUT / "baseline" / f"{set_name}_{reader.model_id if reader else model_id}.jsonl"
    cpath.parent.mkdir(parents=True, exist_ok=True)
    cached = {}
    if cache and cpath.exists():
        for line in cpath.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            cached[r["doc_id"]] = r
    memory: list[str] = []
    results = {}
    for i, d in enumerate(docs):
        if d["doc_id"] in cached and cached[d["doc_id"]]["sha256"] == sha256_file(d["path"]):
            r = cached[d["doc_id"]]
        else:
            if reader is None:
                raise RuntimeError(f"no cached baseline answer for {d['doc_id']}; run with the model loaded")
            c = clients[d["client_id"]]
            t = time.time()
            lines = read_lines(d["path"])
            prompt = BASELINE_PROMPT.format(
                client_name=c["name"], client_ubn=c["ubn"], month=d["month"], **c["policy"],
                memory="\n".join(memory) or "(none yet)",
                ocr="\n".join(f"{l.id}: {l.text}" for l in lines) or "(no text)")
            raw, timing = reader.generate(prompt, Image.open(d["path"]), max_new_tokens=160)
            obj = N.parse_json_loose(raw) or {}
            issues = obj.get("issues") or []
            if isinstance(issues, str):
                issues = [issues]
            r = {"doc_id": d["doc_id"], "sha256": sha256_file(d["path"]), "raw": raw, "parsed": bool(obj),
                 "issues": sorted({x for x in issues if isinstance(x, str) and x in ISSUE_TYPES}),
                 "no": N.clean_str(obj.get("no")), "seller_ubn": N.clean_str(obj.get("seller_ubn")),
                 "date": N.clean_str(obj.get("date")), "total": N.to_int(obj.get("total")),
                 "seconds": round(time.time() - t, 2), "timing": timing, "prompt_tokens_hint": len(prompt)}
            with cpath.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        memory.append(f"{d['client_id']} | {d['month']} | {r['no']} | {r['seller_ubn']} | {r['date']} | {r['total']}")
        results[d["doc_id"]] = r
        if progress:
            progress(i + 1, len(docs), d["doc_id"], r)
    return results


if __name__ == "__main__":
    import argparse
    from ledger_room.reader import Reader
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="main")
    args = ap.parse_args()
    rd = Reader()
    res = run_baseline(args.set, rd, progress=lambda i, n, did, r: print(f"[{i}/{n}] {did} {r['seconds']}s {r['issues']}", flush=True))
    print(len(res), "docs")
