"""Per-run CSV export laid out like the input (進項) records of the VAT media filing file (進項媒體申報檔).

Each document gets the 格式代號 (voucher format code) and 扣抵代號 (deduction code) a bookkeeper would key in, plus a
status that says whether the line can go into this period's filing:

  READY       no blocking exception; deduction code 1, or 3 when Art. 19 makes the input tax non-deductible
  HOLD        a blocking exception (bad UBN, duplicate, date after the period, header/date conflict, unconfirmed read,
              net + tax not equal to the total, buyer UBN unreadable)
  NOT_IN_FILE  no input tax for this client (a taxi receipt, a consumer e-invoice without buyer UBN, another company's
              UBN): an expense voucher, not a VAT input line

Codes used (the bookkeeper must confirm them against the firm's own accounting software before relying on them):
  格式代號 21  三聯式、電子計算機統一發票 (input)
  格式代號 22  二聯式收銀機統一發票、載有稅額之其他憑證 (input)
  格式代號 25  三聯式收銀機統一發票、一般稅額計算之電子發票 (input)
  扣抵代號 1 / 2   deductible purchases and expenses / deductible fixed assets
  扣抵代號 3 / 4   non-deductible purchases and expenses / non-deductible fixed assets
Fixed assets (2 / 4) are not detected: the 記帳士 changes 1 to 2 or 3 to 4 by hand.
This is a CSV for review and import mapping, not the fixed-width media file itself.
"""
from __future__ import annotations

import csv
from pathlib import Path

FORMAT_CODE = {"triplicate_invoice": "21", "e_invoice": "25", "two_part_register": "22"}
BLOCKING = {"UBN_CHECKSUM", "DUP_CROSS_MONTH", "DUP_CROSS_CLIENT", "READ_REVIEW", "AMOUNT_MISMATCH", "BUYER_PRESUMED"}
COLUMNS = ["client_id", "client_ubn", "doc_id", "status", "format_code_格式代號", "deduction_code_扣抵代號",
           "tax_type_課稅別", "invoice_no", "date", "date_roc", "seller_ubn", "buyer_ubn", "net", "tax", "total",
           "exceptions", "note"]


def _roc(d: str | None) -> str:
    if not d:
        return ""
    y, m, dd = d.split("-")
    return f"{int(y) - 1911:03d}{m}{dd}"


def rows(result: dict) -> list[dict]:
    clients = {c["id"]: c for c in result["clients"]}
    ex_by_doc: dict[str, list[dict]] = {}
    for e in result["exceptions"]:
        ex_by_doc.setdefault(e["doc_id"], []).append(e)
    out = []
    for d in result["docs"]:
        f, c = d["fields"], clients[d["client_id"]]
        exs = ex_by_doc.get(d["doc_id"], [])
        ids = sorted({e["rule_id"] for e in exs})
        total = f.get("total")
        tax = f.get("tax") or (total - round(total / 1.05) if total and f.get("doc_type") in FORMAT_CODE else 0)
        buyer = f.get("buyer_ubn")
        code = FORMAT_CODE.get(f.get("doc_type") or "")
        if code is None and tax > 0 and buyer == c["ubn"]:
            code = "22"                     # a receipt that states tax and names the client: 載有稅額之其他憑證
        notes = []
        if code is None:
            status, ded = "NOT_IN_FILE", ""
            notes.append("no input tax for this client: expense voucher only")
        elif not buyer and any(e["rule_id"] == "BUYER_PRESUMED" for e in exs):
            status, ded = "HOLD", ""
            notes.append("buyer UBN not readable; buyer name looks like this client: check the paper")
        elif not buyer:
            status, code, ded = "NOT_IN_FILE", "", ""
            notes.append("no buyer UBN: expense voucher only")
        elif buyer != c["ubn"]:
            status, code, ded = "NOT_IN_FILE", "", ""
            notes.append("buyer UBN is not this client")
        else:
            ded = "3" if "S19_NONDEDUCTIBLE" in ids else "1"
            blocking = [e for e in exs if e["rule_id"] in BLOCKING
                        or (e["rule_id"] == "PERIOD_MISMATCH" and e["severity"] != "warning")]
            status = "HOLD" if blocking else "READY"
            if blocking:
                notes.append("resolve first: " + ", ".join(sorted({e["rule_id"] for e in blocking})))
            if any(e["rule_id"] == "S19_NONDEDUCTIBLE" and e["severity"] == "warning" for e in exs):
                notes.append("Art. 19 to confirm: code 3 unless bought for resale or as a business cost")
            if any(e["rule_id"] == "PERIOD_MISMATCH" and e["severity"] == "warning" for e in exs):
                notes.append("prior-period voucher: deferred claim (Enforcement Rules Art. 29)")
        out.append({
            "client_id": d["client_id"], "client_ubn": c["ubn"], "doc_id": d["doc_id"], "status": status,
            "format_code_格式代號": code, "deduction_code_扣抵代號": ded, "tax_type_課稅別": "1" if code else "",
            "invoice_no": f.get("invoice_no") or "", "date": f.get("date") or "", "date_roc": _roc(f.get("date")),
            "seller_ubn": f.get("seller_ubn") or "", "buyer_ubn": f.get("buyer_ubn") or "",
            "net": f.get("net") if f.get("net") is not None else "", "tax": tax if code else "",
            "total": f.get("total") if f.get("total") is not None else "",
            "exceptions": " ".join(ids), "note": "; ".join(notes),
        })
    return out


def write_csv(result: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:   # BOM so Excel opens the Chinese headers
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows(result))
    return path
