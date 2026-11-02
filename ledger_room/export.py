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
import re as _re
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


# ---------------------------------------------------------------------------------------------------------------
# Fixed-width input (進項) records of the VAT media filing file (營業人進、銷項資料檔, BAN.TXT)
#
# Layout source: 營業稅電子資料申報繳稅作業要點, 第十八點(一) (record length 81, file name = the filer's 8-digit UBN
# + ".TXT"), 第二十點 (field meanings and codes) and 附件五 (field positions), version amended 民國113年04月12日,
# 台財資字第1130001073號令, MOF law system https://law-out.mof.gov.tw/LawContent.aspx?id=GL009478
# (附件五: https://law-out.mof.gov.tw/Download.ashx?FileID=46520&id=GL009478&type=LAW), read on 2026-11-02.
#
# Position (1-based, inclusive)  Field                         Type      What Ledger Room writes
#   1-2    格式代號                    X(2)      21 / 22 / 25 from rows()
#   3-11   申報營業人稅籍編號          X(9)      from the caller's tax_reg_no map (assigned by the tax office;
#                                                 not derivable from the UBN, so it is never guessed)
#  12-18   流水號                      X(7)      0000001, 0000002, ... per file, never repeated
#  19-21   資料所屬年度 (ROC)          9(3)      year of the voucher date
#  22-23   資料所屬月份                9(2)      month of the voucher date
#  24-31   買受人統一編號              X(8)      the client's UBN (an input voucher's buyer is the filer)
#  32-39   銷售人統一編號              X(8)      seller UBN
#  40-49   發票字軌 X(2) + 號碼 9(8)   or 其他憑證號碼 X(10) for a format-22 voucher that is not a uniform invoice
#  50-61   銷售金額                    9(12)     net; for a two-part cash-register invoice the tax-inclusive total
#                                                 (第二十點(十五)1(3))
#  62      課稅別                      X(1)      1 (taxable)
#  63-72   營業稅額                    9(10)     tax; 0 when the amount field already includes tax (第二十點(十七))
#  73      扣抵代號                    X(1)      1 or 3 from rows(); 2 / 4 (fixed assets) are set by the bookkeeper
#  74-78   空白                        X(5)      spaces
#  79      特種稅額稅率                X(1)      space (input record)
#  80      彙加註記／分攤註記          X(1)      space (no aggregation, no sharing)
#  81      通關方式註記                X(1)      space (input record)
#
# Padding: 9(n) fields are right-aligned and zero-filled on the left (stated in 第二十點(十四)(十五)(十七)); X(n) fields
# are left-aligned and space-filled (the usual COBOL picture convention; the regulation does not spell it out).
# The file is ASCII (第十七點). The record separator is CRLF here (MS-DOS / Windows, 第十七點); the regulation does not
# name the separator, so check it against the MOF checking program before relying on it.
# Not verified: this output has not been run through the MOF checking program, imported into accounting software, or
# compared with paper vouchers by a bookkeeper. eval/media_file_check.py checks it against the layout independently.
# ---------------------------------------------------------------------------------------------------------------
MEDIA_RECORD_LEN = 81
MEDIA_SOURCE = ("營業稅電子資料申報繳稅作業要點 第十八、二十點及附件五（民國113年04月12日修正，台財資字第1130001073號令）"
                " https://law-out.mof.gov.tw/LawContent.aspx?id=GL009478")
_UNIFORM_NO = _re.compile(r"^[A-Z]{2}\d{8}$")
_OTHER_NO = _re.compile(r"^[0-9A-Z]{1,10}$")


def _x(v: str, n: int) -> str:
    v = v or ""
    if len(v) > n:
        raise ValueError(f"{v!r} longer than X({n})")
    return v.ljust(n)


def _9(v: int, n: int) -> str:
    if v is None or int(v) < 0:
        raise ValueError(f"9({n}) field needs a non-negative integer, got {v!r}")
    s = str(int(v))
    if len(s) > n:
        raise ValueError(f"{v!r} longer than 9({n})")
    return s.zfill(n)


def media_record(*, fmt: str, tax_reg_no: str, serial: int, date: str, buyer_ubn: str, seller_ubn: str,
                 voucher_no: str, amount: int, tax: int, deduction: str, tax_type: str = "1") -> str:
    """One 81-character input record. Raises ValueError for anything the layout cannot hold."""
    if not _re.fullmatch(r"\d{9}", tax_reg_no or ""):
        raise ValueError(f"稅籍編號 must be 9 digits, got {tax_reg_no!r}")
    y, m, _ = date.split("-")
    if _UNIFORM_NO.match(voucher_no or ""):
        voucher = voucher_no                     # 發票字軌 X(2) + 發票(起)號碼 9(8)
    elif fmt == "22" and _OTHER_NO.match(voucher_no or ""):
        voucher = _x(voucher_no, 10)             # 其他憑證號碼 X(10)
    else:
        raise ValueError(f"voucher number {voucher_no!r} does not fit format {fmt}")
    rec = (_x(fmt, 2) + _x(tax_reg_no, 9) + _9(serial, 7) + _9(int(y) - 1911, 3) + _9(int(m), 2)
           + _x(buyer_ubn, 8) + _x(seller_ubn, 8) + voucher + _9(amount, 12) + _x(tax_type, 1) + _9(tax, 10)
           + _x(deduction, 1) + " " * 5 + " " + " " + " ")
    assert len(rec) == MEDIA_RECORD_LEN, len(rec)
    return rec


def media_records(result: dict, tax_reg_no: dict[str, str], *, include_hold: bool = False
                  ) -> tuple[dict[str, list[str]], list[dict]]:
    """Input records per client UBN, from the same rows() as the CSV.

    Only READY lines go in (HOLD too when include_hold, for a dry look at the layout); NOT_IN_FILE never does.
    tax_reg_no maps client UBN -> 9-digit 稅籍編號. Returns ({client_ubn: [record, ...]}, skipped), where skipped
    lists lines left out with the reason (missing date, amount or voucher number), so nothing is dropped silently.
    """
    docs = {d["doc_id"]: d for d in result["docs"]}
    out: dict[str, list[str]] = {}
    skipped: list[dict] = []
    for r in rows(result):
        if r["status"] == "NOT_IN_FILE" or (r["status"] == "HOLD" and not include_hold):
            continue
        cu = r["client_ubn"]
        if cu not in tax_reg_no:
            raise ValueError(f"no 稅籍編號 for client UBN {cu}; pass it in tax_reg_no (never guessed)")
        f = docs[r["doc_id"]]["fields"]
        two_part = r["format_code_格式代號"] == "22" and f.get("doc_type") == "two_part_register"
        amount = f.get("total") if two_part else f.get("net")
        tax = 0 if two_part else r["tax"]
        reason = None
        if not r["date"]:
            reason = "no date"
        elif amount is None or tax in ("", None):
            reason = "no amount or tax"
        try:
            if reason is None:
                rec = media_record(fmt=r["format_code_格式代號"], tax_reg_no=tax_reg_no[cu],
                                   serial=len(out.get(cu, [])) + 1, date=r["date"], buyer_ubn=r["buyer_ubn"],
                                   seller_ubn=r["seller_ubn"], voucher_no=(r["invoice_no"] or "").upper(),
                                   amount=int(amount), tax=int(tax), deduction=r["deduction_code_扣抵代號"])
                out.setdefault(cu, []).append(rec)
        except ValueError as e:
            reason = str(e)
        if reason:
            skipped.append({"doc_id": r["doc_id"], "status": r["status"], "reason": reason})
    return out, skipped


def write_media_txt(result: dict, out_dir: Path, tax_reg_no: dict[str, str], *, include_hold: bool = False,
                    newline: str = "\r\n") -> tuple[list[Path], list[dict]]:
    """Write one <client UBN>.TXT per client (第十八點(一): file name = the filer's UBN). ASCII, 81 characters a line."""
    recs, skipped = media_records(result, tax_reg_no, include_hold=include_hold)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for ubn, lines in sorted(recs.items()):
        p = out_dir / f"{ubn}.TXT"
        p.write_bytes("".join(line + newline for line in lines).encode("ascii"))
        paths.append(p)
    return paths, skipped


def main(argv=None):
    """python -m ledger_room.export data/out/runs/<run id> --tax-reg taxreg.json [--out DIR] [--include-hold]
    taxreg.json: {"<client UBN>": "<9-digit 稅籍編號>", ...} from the firm's own client records."""
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Write <client UBN>.TXT input records (81 characters) for a run")
    ap.add_argument("run_dir")
    ap.add_argument("--tax-reg", required=True, help="JSON map of client UBN -> 9-digit tax registration number")
    ap.add_argument("--out", default=None, help="default: <run_dir>/media_txt")
    ap.add_argument("--include-hold", action="store_true", help="also write HOLD lines (layout check only, not for filing)")
    a = ap.parse_args(argv)
    run = Path(a.run_dir)
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    tax_reg = json.loads(Path(a.tax_reg).read_text(encoding="utf-8"))
    paths, skipped = write_media_txt(result, Path(a.out) if a.out else run / "media_txt", tax_reg,
                                     include_hold=a.include_hold)
    for p in paths:
        print(p)
    for s in skipped:
        print("skipped", s["doc_id"], s["status"], s["reason"])


if __name__ == "__main__":
    main()
