"""Independent checker for the VAT media filing file (營業人進、銷項資料檔, <UBN>.TXT), 81 characters a record.

python -m eval.media_file_check 67094646.TXT [more.TXT ...] [--json]

The layout below is typed in from the official table on its own (it does not import ledger_room/export.py), so a
mistake in the writer is not copied into the check:
  營業稅電子資料申報繳稅作業要點, 第十七點 (ASCII), 第十八點(一) (81 characters, file name = UBN + .TXT),
  第二十點 (field meanings and codes) and 附件五 (positions), amended 民國113年04月12日, 台財資字第1130001073號令.
  https://law-out.mof.gov.tw/LawContent.aspx?id=GL009478
  附件五: https://law-out.mof.gov.tw/Download.ashx?FileID=46520&id=GL009478&type=LAW

Errors are layout violations (length, character type, code ranges, duplicate serials, UBN checksum). Warnings are
plausibility checks that the regulation does not state as a hard rule (tax about 5% of the amount on formats 21 / 25,
buyer UBN equal to the file name on input records). This is not the MOF checking program (檢核程式); passing here
does not mean the MOF program accepts the file.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ledger_room.ubn import is_valid_new

RECORD_LEN = 81
# (name, picture, start, end) from 附件五, 1-based inclusive positions. Shared positions are listed once per meaning.
LAYOUT = [
    ("格式代號", "X", 1, 2),
    ("申報營業人稅籍編號", "X", 3, 11),
    ("流水號", "X", 12, 18),
    ("資料所屬年度", "9", 19, 21),
    ("資料所屬月份", "9", 22, 23),
    ("買受人統一編號/發票訖號", "X", 24, 31),
    ("銷售人統一編號/彙總張數+空白/空白+海關", "X", 32, 39),
    ("發票字軌+號碼/其他憑證號碼/載具流水號/海關", "X", 40, 49),
    ("銷售金額/營業稅稅基", "9", 50, 61),
    ("課稅別", "X", 62, 62),
    ("營業稅額", "9", 63, 72),
    ("扣抵代號", "X", 73, 73),
    ("空白", "X", 74, 78),
    ("特種稅額稅率", "X", 79, 79),
    ("彙加註記/分攤註記", "X", 80, 80),
    ("通關方式註記", "X", 81, 81),
]
INPUT_FORMATS = {str(n) for n in range(21, 30)}      # 21-29 進項
OUTPUT_FORMATS = {str(n) for n in range(31, 39)}     # 31-38 銷項
TAX_TYPES = {"1", "2", "3", "F", "D"}                # 第二十點(十六)
DEDUCTION = {"1", "2", "3", "4"}                     # 第二十點(十八); blank only for output records
SPECIAL_RATE = {" ", "1", "2", "3", "4", "5", "6", "7"}   # 第二十點(十九)
AGG = {" ", "A", "B"}                                # 第二十點(二十); B only with format 25
CUSTOMS = {" ", "1", "2"}                            # 第二十點(二十一); blank for input records
UNIFORM_NO = re.compile(r"^[A-Z]{2}\d{8}$")
CARRIER_NO = re.compile(r"^BB[0-9A-Z]{8}$")          # 第二十點(十二)
UNIFORM_INVOICE_INPUT = {"21", "23", "25", "26"}     # always a uniform invoice number in 40-49 (26: first number)


def field(rec: str, start: int, end: int) -> str:
    return rec[start - 1:end]


def check_record(rec: str, line_no: int = 1, filer_ubn: str | None = None) -> tuple[list[dict], list[dict]]:
    errs: list[dict] = []
    warns: list[dict] = []

    def err(f, msg):
        errs.append({"line": line_no, "field": f, "error": msg})

    def warn(f, msg):
        warns.append({"line": line_no, "field": f, "warning": msg})

    if len(rec) != RECORD_LEN:
        err("record", f"length {len(rec)}, must be {RECORD_LEN}")
        return errs, warns
    if not rec.isascii():
        err("record", "non-ASCII characters (第十七點: ASCII)")
        return errs, warns
    for name, pic, a, b in LAYOUT:
        v = field(rec, a, b)
        if pic == "9" and not v.isdigit():
            err(name, f"9({b - a + 1}) must be digits, got {v!r}")
    fmt = field(rec, 1, 2)
    is_in, is_out = fmt in INPUT_FORMATS, fmt in OUTPUT_FORMATS
    if not (is_in or is_out):
        err("格式代號", f"{fmt!r} is not 21-29 or 31-38")
    if not field(rec, 3, 11).isdigit():
        err("申報營業人稅籍編號", "must be 9 digits")
    if not re.fullmatch(r"[0-9A-Za-z]{7}", field(rec, 12, 18)):
        err("流水號", "must be 7 letters or digits")
    month = field(rec, 22, 23)
    if month.isdigit() and not 1 <= int(month) <= 12:
        err("資料所屬月份", f"{month} is not 01-12")
    tax_type = field(rec, 62, 62)
    if tax_type not in TAX_TYPES:
        err("課稅別", f"{tax_type!r} not in 1 2 3 F D")
    ded = field(rec, 73, 73)
    if is_in and ded not in DEDUCTION:
        err("扣抵代號", f"input record needs 1-4, got {ded!r}")
    if is_out and ded != " ":
        err("扣抵代號", "must be blank on an output record")
    if field(rec, 74, 78) != " " * 5:
        err("空白", "positions 74-78 must be spaces")
    if field(rec, 79, 79) not in SPECIAL_RATE:
        err("特種稅額稅率", f"{field(rec, 79, 79)!r} not blank or 1-7")
    if is_in and field(rec, 79, 79) != " ":
        err("特種稅額稅率", "must be blank on an input record")
    agg = field(rec, 80, 80)
    if agg not in AGG:
        err("彙加註記/分攤註記", f"{agg!r} not blank, A or B")
    if agg == "B" and fmt != "25":
        err("彙加註記/分攤註記", "B (分攤) is only allowed with format 25")
    cust = field(rec, 81, 81)
    if cust not in CUSTOMS:
        err("通關方式註記", f"{cust!r} not blank, 1 or 2")
    if is_in and cust != " ":
        err("通關方式註記", "must be blank on an input record")

    if is_in and tax_type not in ("F", "D"):
        buyer, seller, no = field(rec, 24, 31), field(rec, 32, 39), field(rec, 40, 49)
        if fmt in ("28", "29"):                                   # customs: 32-35 blank, 36-49 certificate number
            if field(rec, 32, 35) != "    ":
                err("空白", "positions 32-35 must be spaces for format 28 / 29")
            if not field(rec, 36, 49).strip():
                err("海關代徵營業稅繳納證號碼", "missing")
        elif agg == "A":                                          # aggregated input: 32-35 count, 36-39 blank
            if not field(rec, 32, 35).isdigit():
                err("彙總張數", "9(4) must be digits")
            if field(rec, 36, 39) != "    ":
                err("空白", "positions 36-39 must be spaces when 彙加註記 = A")
        else:
            if not re.fullmatch(r"\d{8}", seller):
                err("銷售人統一編號", f"must be 8 digits, got {seller!r}")
            elif not is_valid_new(seller):
                err("銷售人統一編號", f"{seller} fails the UBN checksum (2023 rule)")
        if buyer.strip():
            if not re.fullmatch(r"\d{8}", buyer):
                err("買受人統一編號", f"must be 8 digits, got {buyer!r}")
            elif not is_valid_new(buyer):
                err("買受人統一編號", f"{buyer} fails the UBN checksum (2023 rule)")
            elif filer_ubn and buyer != filer_ubn:
                warn("買受人統一編號", f"{buyer} differs from the file name {filer_ubn} (fine only for a head office "
                                  "filing for its branches)")
        else:
            err("買受人統一編號", "blank on an input record")
        if fmt in UNIFORM_INVOICE_INPUT and not UNIFORM_NO.match(no):
            err("發票字軌+號碼", f"format {fmt} needs 2 capital letters + 8 digits, got {no!r}")
        if fmt in ("22", "24", "27") and not (UNIFORM_NO.match(no) or CARRIER_NO.match(no)
                                               or re.fullmatch(r"[0-9A-Z]{1,10} *", no)):
            err("其他憑證號碼", f"must be a uniform invoice number or up to 10 capital letters / digits, got {no!r}")
        amt, tax = field(rec, 50, 61), field(rec, 63, 72)
        if fmt in ("21", "25") and amt.isdigit() and tax.isdigit() and int(amt) > 0:
            if abs(int(tax) - round(int(amt) * 0.05)) > 1:
                warn("營業稅額", f"tax {int(tax)} is not about 5% of {int(amt)}")
    return errs, warns


def check_file(path: Path) -> dict:
    path = Path(path)
    errs: list[dict] = []
    warns: list[dict] = []
    stem_ok = re.fullmatch(r"\d{8}", path.stem) is not None and path.suffix.upper() == ".TXT"
    if not stem_ok:
        errs.append({"line": 0, "field": "file name", "error": "must be the filer's 8-digit UBN + .TXT (第十八點(一))"})
    data = path.read_bytes()
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as e:
        return {"file": str(path), "records": 0, "ok": False,
                "errors": errs + [{"line": 0, "field": "file", "error": f"not ASCII ({e})"}], "warnings": []}
    lines = text.splitlines()
    if lines and lines[-1] == "":
        lines = lines[:-1]
    seen: dict[str, int] = {}
    for i, rec in enumerate(lines, 1):
        e, w = check_record(rec, i, path.stem if stem_ok else None)
        errs += e
        warns += w
        if len(rec) == RECORD_LEN:
            s = field(rec, 12, 18)
            if s in seen:
                errs.append({"line": i, "field": "流水號", "error": f"{s} repeats line {seen[s]} (第二十點(三))"})
            seen.setdefault(s, i)
    if not lines:
        errs.append({"line": 0, "field": "file", "error": "no records"})
    return {"file": str(path), "records": len(lines), "ok": not errs, "errors": errs, "warnings": warns}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    reps = [check_file(Path(f)) for f in a.files]
    if a.json:
        print(json.dumps(reps, ensure_ascii=False, indent=1))
    else:
        for r in reps:
            print(f"{r['file']}: {r['records']} records, {len(r['errors'])} errors, {len(r['warnings'])} warnings"
                  f" -> {'PASS' if r['ok'] else 'FAIL'}")
            for e in r["errors"][:50]:
                print(f"  line {e['line']} {e['field']}: {e['error']}")
            for w in r["warnings"][:20]:
                print(f"  (warning) line {w['line']} {w['field']}: {w['warning']}")
    return 0 if all(r["ok"] for r in reps) else 1


if __name__ == "__main__":
    sys.exit(main())
