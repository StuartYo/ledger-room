"""Independent checker for a client's full filing pair: <UBN>.TXT (media file) and <UBN>.TET_U (return data file).

python -m eval.filing_check out/67094646.TXT out/67094646.TET_U [--json]

The TET_U layout is typed in again from 附件六 here (it does not import ledger_room/filing.py), so a slip in the writer
is not copied into the check:
  營業稅電子資料申報繳稅作業要點 第十八點(二), 第二十一點, 附件六 (amended 民國113年04月12日)
  https://law-out.mof.gov.tw/LawContent.aspx?id=GL009478
  附件六: https://law-out.mof.gov.tw/Download.ashx?FileID=51326&id=GL009478&type=LAW
The box arithmetic is from the 401 / 403 forms' 填寫說明 (MOF e-tax download, 401、403及404 A4 PDF).

Checks:
  TXT    eval.media_file_check.check_file (附件五 layout), plus: serials 0000001.. in file order.
  TET_U  UTF-8 without BOM, CRLF, 112 fields (111 "|"), each field's picture and width, S9 last-character sign
         codes, header codes (資料別, 所屬年月, 申報代號 vs 總繳代號, 申報種類, 縣市別, 自行或委任), the form's own
         sums (21, 22, 23, 44-47, 106, 110-115, 107 = 45 + 47 on a 401, 51 = (45 + 47) x (1 - 50) on a 403).
  Cross  recomputes from the TXT lines, on its own, the sales boxes 1/2, 5/6, 9/10, 17/18, 7, 15 and the input boxes
         28-43, 78-81, and compares them with the TET_U (tax-inclusive lines totalled and split at 5/105).
This is not the MOF checking program; passing here does not mean the MOF program accepts the files.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from eval.media_file_check import check_file

# field no -> (picture, width, box code or None); 附件六
_S12, _S10 = ("S", 12), ("S", 10)
LAYOUT: dict[int, tuple[str, int, int | None]] = {
    1: ("X", 1, None), 2: ("X", 8, None), 3: ("X", 8, None), 4: ("X", 5, None), 5: ("X", 1, None),
    6: ("X", 9, None), 7: ("X", 1, None), 8: ("9", 10, None),
    96: ("X", 1, None), 97: ("X", 1, None), 98: ("X", 1, None), 99: ("X", 10, None), 100: ("C", 12, None),
    101: ("X", 4, None), 102: ("X", 11, None), 103: ("X", 5, None), 104: ("C", 50, None),
    72: ("9", 3, 50),
}
_codes = {
    9: 1, 10: 5, 11: 9, 12: 13, 13: 17, 14: 21, 15: 2, 16: 6, 17: 10, 18: 14, 19: 18, 20: 22, 21: 82,
    22: 7, 23: 15, 24: 19, 25: 23, 26: 4, 27: 8, 28: 12, 29: 16, 30: 20, 31: 24, 32: 52, 33: 53, 34: 54, 35: 55,
    36: 56, 37: 57, 38: 58, 39: 59, 40: 60, 41: 61, 42: 62, 43: 63, 44: 64, 45: 65, 46: 66, 47: 25, 48: 26, 49: 27,
    50: 28, 51: 30, 52: 32, 53: 34, 54: 36, 55: 38, 56: 40, 57: 42, 58: 44, 59: 46,
    60: 29, 61: 31, 62: 33, 63: 35, 64: 37, 65: 39, 66: 41, 67: 43, 68: 45, 69: 47, 70: 48, 71: 49, 73: 51,
    74: 78, 75: 80, 76: 73, 77: 74, 78: 79, 79: 81, 80: 75, 81: 76, 82: 101, 83: 103, 84: 104, 85: 105, 86: 106,
    87: 107, 88: 108, 89: 109, 90: 110, 91: 111, 92: 112, 93: 113, 94: 114, 95: 115,
    105: 67, 106: 69, 107: 71, 108: 68, 109: 70, 110: 72, 111: 84, 112: 85}
_TAX_WIDTH_10 = {2, 6, 10, 14, 18, 22, 53, 55, 57, 59, 61, 64, 66, 29, 31, 33, 35, 37, 39, 41, 43, 45, 47, 51, 79,
                 81, 75, 76, 101, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 68, 70, 72, 85}
for _no, _code in _codes.items():
    LAYOUT[_no] = ("S", 10 if _code in _TAX_WIDTH_10 else 12, _code)
POS, NEG = "{ABCDEFGHI", "}JKLMNOPQR"
COUNTY = set("ABCDEFGHIJKMNOPQTUVWXZ")    # 附件七


def decode_signed(s: str) -> int | None:
    if not s or not s[:-1].isdigit():
        return None
    last = s[-1]
    if last in POS:
        return int(s[:-1] + str(POS.index(last)))
    if last in NEG:
        return -int(s[:-1] + str(NEG.index(last)))
    return None


def check_tet_u(path: Path) -> dict:
    errs: list[str] = []
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        errs.append("UTF-8 BOM present (附件六 說明(1): 無BOM)")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        return {"file": str(path), "ok": False, "errors": [f"not UTF-8 ({e})"], "boxes": {}}
    if "\n" in text.replace("\r\n", ""):
        errs.append("line break other than CRLF")
    recs = [x for x in text.split("\r\n") if x]
    if len(recs) != 1:
        errs.append(f"{len(recs)} records; one return per file expected")
    if not recs:
        return {"file": str(path), "ok": False, "errors": errs + ["empty"], "boxes": {}}
    vals = recs[0].split("|")
    if len(vals) != 112:
        return {"file": str(path), "ok": False, "errors": errs + [f"{len(vals)} fields, must be 112"], "boxes": {}}
    f = {i + 1: v for i, v in enumerate(vals)}
    boxes: dict[int, int] = {}
    for no, (pic, w, code) in sorted(LAYOUT.items()):
        v = f[no]
        if v != v.strip():
            errs.append(f"field {no}: leading or trailing spaces")
        if pic == "S":
            n = decode_signed(v) if len(v) == w else None
            if n is None:
                errs.append(f"field {no}: {v!r} is not S9({w})")
            else:
                boxes[code] = n
        elif pic == "9":
            if not (len(v) == w and v.isdigit()):
                errs.append(f"field {no}: {v!r} is not 9({w})")
            elif code is not None:
                boxes[code] = int(v)
        else:
            if len(v) > w:
                errs.append(f"field {no}: longer than {pic}({w})")
            if pic == "X" and not v.isascii():
                errs.append(f"field {no}: X field with non-ASCII text")
    if f[1] not in ("1", "3", "4"):
        errs.append(f"資料別 {f[1]!r}")
    if not re.fullmatch(r"[0-9A-Za-z]{8}", f[2]):
        errs.append("檔案編號 must be 8 letters or digits")
    if not re.fullmatch(r"\d{8}", f[3]):
        errs.append("統一編號 must be 8 digits")
    if not (re.fullmatch(r"\d{5}", f[4]) and 1 <= int(f[4][3:]) <= 12):
        errs.append(f"所屬年月 {f[4]!r}")
    if f[7] not in ("0", "1", "2") or f[5] != ("5" if f[7] == "1" else "1"):
        errs.append(f"申報代號 {f[5]!r} does not match 總繳代號 {f[7]!r}")
    if not re.fullmatch(r"\d{9}", f[6]):
        errs.append("稅籍編號 must be 9 digits")
    if f[96] not in ("1", "2"):
        errs.append("申報種類 must be 1 or 2")
    if f[97] not in COUNTY:
        errs.append(f"縣市別 {f[97]!r} not in 附件七")
    if f[98] not in ("1", "2"):
        errs.append("自行或委任 must be 1 or 2")
    if f[98] == "2" and not f[104]:
        errs.append("委任申報 without 代理申報人登錄字號")
    b = boxes
    if len(b) == len([1 for p in LAYOUT.values() if p[2] is not None]):
        sums = {
            21: b[1] + b[5] + b[9] + b[13] - b[17], 22: b[2] + b[6] + b[10] + b[14] - b[18], 23: b[7] + b[15] - b[19],
            44: b[28] + b[32] + b[36] + b[78] - b[40], 45: b[29] + b[33] + b[37] + b[79] - b[41],
            46: b[30] + b[34] + b[38] + b[80] - b[42], 47: b[31] + b[35] + b[39] + b[81] - b[43],
            106: b[101] + b[103] + b[104] + b[105], 110: b[107] + b[108] + b[109],
            111: max(b[106] - b[110], 0), 112: max(b[110] - b[106], 0),
        }
        if f[1] == "1":
            sums[107] = b[45] + b[47]
        elif f[1] == "3":
            sums[107] = b[51]
            exact = (b[45] + b[47]) * (100 - b[50])
            if abs(b[51] * 100 - exact) > 50:
                errs.append(f"51 = {b[51]} is not (45 + 47) x (1 - {b[50]}%)")
        for code, want in sums.items():
            if b[code] != want:
                errs.append(f"代號 {code} = {b[code]}, the form's sum gives {want}")
        if abs(b[113] * 100 - (b[23] * 5 + b[47] * 100)) > 50:
            errs.append(f"代號 113 = {b[113]}, want 23 x 5% + 47")
        want114 = (b[113] if b[112] > b[113] else b[112]) if b[112] > 0 else 0
        if b[114] != want114 or b[115] != b[112] - b[114]:
            errs.append("代號 114 / 115 do not follow the form's rule")
    return {"file": str(path), "ok": not errs, "errors": errs, "boxes": boxes, "header": {k: f[k] for k in range(1, 9)}}


def _split(total: int) -> tuple[int, int]:
    tax = (total * 5 * 2 + 105) // 210          # total x 5 / 105, half up
    return total - tax, tax


def boxes_from_txt(path: Path) -> dict[int, int]:
    """Sales and input boxes recomputed from the 81-character lines (附件五 positions typed in here)."""
    acc: dict[int, list[int]] = {}               # amount box -> [exclusive amount, exclusive tax, inclusive total]
    zero = {7: 0, 15: 0, 19: 0}
    sales = {"31": 1, "35": 5, "32": 9, "36": 13, "33": 17, "34": 17}
    inp = {"21": 28, "26": 28, "25": 32, "22": 36, "27": 36, "28": 78, "23": 40, "24": 40, "29": 40}
    for line in Path(path).read_bytes().decode("ascii").split("\r\n"):
        if len(line) != 81:
            continue
        fmt, tt, amt, tax, ded, cust = line[0:2], line[61], int(line[49:61]), int(line[62:72]), line[72], line[80]
        if tt in ("F", "D"):
            continue
        if fmt in sales:
            if tt == "2":
                zero[19 if fmt in ("33", "34") else 7 if cust == "1" else 15] += amt
                continue
            if tt != "1":
                continue
            box = sales[fmt]
        elif fmt in inp and ded in ("1", "2"):
            box = inp[fmt] + (2 if ded == "2" else 0)
        else:
            continue
        a = acc.setdefault(box, [0, 0, 0])
        if tt == "1" and tax == 0 and amt > 0 and fmt not in ("28", "29"):
            a[2] += amt
        else:
            a[0] += amt
            a[1] += tax
    out = dict(zero)
    for box in (1, 5, 9, 13, 17, 28, 30, 32, 34, 36, 38, 40, 42, 78, 80):
        e_amt, e_tax, incl = acc.get(box, [0, 0, 0])
        ia, it = _split(incl) if incl else (0, 0)
        out[box], out[box + 1] = e_amt + ia, e_tax + it
    return out


def check_pair(txt: Path, tet: Path) -> dict:
    t = check_file(txt)
    errs = [f"TXT line {e['line']} {e['field']}: {e['error']}" for e in t["errors"]]
    lines = [x for x in Path(txt).read_bytes().decode("ascii", "replace").split("\r\n") if x]
    if [x[11:18] for x in lines] != [f"{i:07d}" for i in range(1, len(lines) + 1)]:
        errs.append("TXT serials are not 0000001.. in file order")
    u = check_tet_u(tet)
    errs += [f"TET_U {e}" for e in u["errors"]]
    if Path(txt).stem != u.get("header", {}).get(3):
        errs.append("TXT file name and TET_U 統一編號 differ")
    if u["boxes"]:
        for box, want in boxes_from_txt(txt).items():
            if u["boxes"].get(box) != want:
                errs.append(f"代號 {box}: TET_U {u['boxes'].get(box)}, recomputed from the TXT {want}")
    return {"txt": str(txt), "tet_u": str(tet), "ok": not errs, "errors": errs, "txt_warnings": t["warnings"],
            "records": t["records"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("txt")
    ap.add_argument("tet_u")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = check_pair(Path(a.txt), Path(a.tet_u))
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(f"{rep['txt']} + {rep['tet_u']}: {rep['records']} records, {len(rep['errors'])} errors"
              f" -> {'PASS' if rep['ok'] else 'FAIL'}")
        for e in rep["errors"][:50]:
            print("  " + e)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
