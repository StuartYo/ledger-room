"""Full VAT filing output for one client and period: the media file (<UBN>.TXT, input and sales lines together) and
the return data file (<UBN>.TET_U, form 401 or 403), with the box totals computed from the lines.

Sources (all read 2026-11-07):
  [要點]  營業稅電子資料申報繳稅作業要點, amended 民國113年04月12日 (台財資字第1130001073號令)
          https://law-out.mof.gov.tw/LawContent.aspx?id=GL009478
          第十一點(四): BAN.TXT and BAN.TET_U must pass the MOF checking program before upload.
          第十八點(一)(二): <UBN>.TXT, 81 characters a record; <UBN>.TET_U, 112 fields separated by "|".
          第二十點 + 附件五: the 81-character layout; 第二十點(三): serial numbers in order, never repeated (no rule
          on the order of records). 第二十點(十五)(十七): a sales amount that includes tax carries tax 0; the return
          totals such lines and splits them: tax = total / 1.05 * 0.05 rounded, amount = total - tax. Two-part
          cash-register invoices and transport tickets on the input side work the same way.
          第二十一點 + 附件六 (https://law-out.mof.gov.tw/Download.ashx?FileID=51326&id=GL009478&type=LAW):
          the TET_U layout: UTF-8 without BOM, CRLF between records, numbers zero-filled on the left, no spaces
          around values; S9 numbers carry the sign in the last digit (+0..9 = { A..I, -0..9 = } J..R).
          資料別 1 = 401, 3 = 403, 4 = 404; 所屬年月 = the period's last month (YYYMM); 申報代號 1 when 總繳代號 is
          0 or 2, 5 when it is 1; 申報種類 1 = by period, 2 = by month; 自行或委任 1 / 2; 縣市別 in 附件七.
  [表]    營業人銷售額與稅額申報書（401、403及404）及填寫說明, MOF e-tax download
          https://www.etax.nat.gov.tw/etwmain/api/functions/etw212w/download/189b50a490600000c0cddfef1a752804/
          (file 營業人銷售額與稅額申報書及說明欄(401、403及404)A4格式.pdf): which voucher goes in which box, the
          合計 lines (1-4 plus 78 minus 5), 107 = 45 + 47 (401), 113 = 23 x 5% + 47, 114 / 115, and for 403:
          不得扣抵比例 50 = (④+⑤-⑧) / (⑦-⑧) with the fraction of a percent dropped, 51 = (45 + 47) x (1 - 50).
  [兼營]  兼營營業人營業稅額計算辦法 (amended 民國95年12月21日) https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=G0340085
          Art. 3: the ratio is exempt (and special-rate) net sales over all net sales, land and securities left out;
          Art. 4: per-period proportional deduction; Art. 7: the last period of the year is adjusted with the year's
          ratio: 調整稅額 = 當年度已扣抵之進項稅額 - (當年度進項稅額 - 依第19條第1項不得扣抵之進項稅額) x (1 - 當年度
          不得扣抵比例). VAT Act Art. 19(3) authorises it.

What is computed, and what is not:
  - Box codes are the form's 代號 (1, 2, ... 115). Input lines go to the 401 / 403 boxes by format code (21/26 ->
    28-31, 25 -> 32-35, 22/27 -> 36-39, 28 -> 78-81, 23/24/29 -> 40-43 as a deduction) and deduction code (1 / 3
    進貨及費用, 2 / 4 固定資產); only codes 1 and 2 count as deductible, codes 3 and 4 only count in 進項總金額 (48 / 49).
  - Not supported, raise NotSupported: form 404, special-rate (特種稅額) sales lines (formats 37 / 38 or a 特種稅額
    稅率), purchases of foreign services (74-76, 103), and the direct-deduction method (直接扣抵法).
  - 403 per-period ratio is computed; the year-end adjustment (Art. 7) is a separate function, annual_adjustment(),
    whose result the caller passes in (it needs the whole year's returns); without it the last period of the year
    gets a note and 105 / 109 stay 0.
  - Inputs Ledger Room cannot derive from the lines and the caller must pass: 上期累積留抵稅額 (108, from last return's
    115), land / fixed-asset sales (26 / 27), plain receipts from small-scale businesses (part of 48), filer details.

Not verified (read this before relying on it):
  - Nothing here has been run through the MOF checking program (see the report for its name and download page),
    imported into accounting software, or compared with a return a bookkeeper filed.
  - 檔案編號 (field 2): 第二十一點(二) only says "8 alphanumeric characters, zero-filled"; its meaning is not stated,
    so the default "00000000" is a placeholder the checking program may reject.
  - Whether S9 zero must be written "...0{" (sign in the last digit even for zero and positive values): this follows
    the rule as written; the checking program decides.
  - 進項總金額 (48 / 49): returns and allowances are subtracted and customs-certificate tax bases are included; the
    form's note only says "金額總數" of purchase vouchers, so both choices are my reading.
  - The record order of the media file: the regulation sets none; this writes format code, then data month, then
    voucher number, and renumbers the serials 0000001.. in that order.
  - Rounding: "四捨五入" (half up) is used wherever the regulation or form computes a tax from a total; for 51 and the
    year-end adjustment the rounding is not stated and half up is my choice.
  - A sales record with tax 0 and a positive taxable amount is read as tax-inclusive (第二十點(十七)); a business
    sale small enough that its tax rounds to 0 would be split wrongly (at most a few NT dollars).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from . import export
from . import einvoice_platform as plat

TET_U_FIELDS = 112
COUNTY_CODES = set("ABCDEFGHIJKMNOPQTUVWXZ")   # 附件七 (letters printed full-width there; the field is X(001))


class NotSupported(Exception):
    """A filing case Ledger Room does not compute (form 404, special-rate sales, foreign services, direct deduction)."""


def client_form(client: dict) -> str:
    """The client's return form from its config: "form": "401" (default) or "403"; "404" raises NotSupported."""
    f = str(client.get("form", "401"))
    if f == "404":
        raise NotSupported("form 404 (專營特種稅額計算營業人) is not supported: special tax rates are out of scope")
    if f not in ("401", "403"):
        raise ValueError(f"client form must be 401 or 403, got {f!r}")
    return f


def _half_up(x) -> int:
    """Round a Fraction / int half away from zero (四捨五入), exactly."""
    q = Fraction(x)
    n = abs(q.numerator) * 2 + q.denominator
    v = n // (2 * q.denominator)
    return v if q >= 0 else -v


def split_inclusive(total: int) -> tuple[int, int]:
    """(amount, tax) of a tax-inclusive total at 5%: tax = total / 1.05 x 0.05 rounded half up (第二十點(十五))."""
    tax = _half_up(Fraction(total) * 5 / 105)
    return total - tax, tax


# ---------------------------------------------------------------------------------------------------- media file

@dataclass
class MediaFile:
    client_ubn: str
    tax_reg_no: str
    lines: list[str]
    sources: list[str]                     # per line: platform-sales, sales-import, ledger-room, platform-input
    records: list[dict]                    # parsed lines, same order
    duplicates: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def filename(self) -> str:
        return f"{self.client_ubn}.TXT"


def _voucher_key(r: dict):
    if r["invoice_no"]:
        return ("inv", r["invoice_no"])
    if r["customs_no"]:
        return ("customs", r["customs_no"])
    if r["other_voucher_no"]:
        return ("other", r["other_voucher_no"], r["seller_ubn"])
    return None


def _client_result(result: dict, ubn: str) -> dict:
    ids = {c["id"] for c in result["clients"] if c.get("ubn") == ubn}
    sub = dict(result)
    sub["clients"] = [c for c in result["clients"] if c["id"] in ids]
    sub["docs"] = [d for d in result["docs"] if d["client_id"] in ids]
    sub["exceptions"] = [e for e in result.get("exceptions", []) if e["client_id"] in ids]
    return sub


def assemble_media_file(client: dict, period, *, tax_reg_no: str, result: dict | None = None,
                        platform: dict | None = None, sales_lines=(), deduction_overrides: dict | None = None,
                        include_hold: bool = False) -> MediaFile:
    """The client's media file for the period: sales lines from the platform download (and any other sales import),
    Ledger Room's input lines (export.media_records: READY only unless include_hold), and the platform's input lines
    that no photographed voucher already covers.

    platform: platform.load_client(client, period, ...) output; None loads data/platform/<UBN>/.
    sales_lines: extra 81-character sales records (e.g. a sales file from the client's POS or billing software).
    deduction_overrides: {voucher number: "1".."4"}, e.g. a computer bought from a platform e-invoice -> "2".
    When a photographed voucher and a platform input line have the same number, the photographed line is kept (it
    went through the rules) and the pair is listed in .duplicates with any field that differs."""
    ubn = plat._client_ubn(client)
    if not re.fullmatch(r"\d{9}", tax_reg_no or ""):
        raise ValueError(f"稅籍編號 must be 9 digits, got {tax_reg_no!r} (assigned by the tax office, never guessed)")
    if platform is None:
        platform = plat.load_client(client, period)
    if platform["client_ubn"] != ubn:
        raise ValueError(f"platform data is for {platform['client_ubn']}, not {ubn}")
    notes = [f"platform file problem: {e['file']} line {e['line']}: {e['error']}" for e in platform["errors"]]
    entries: list[tuple[str, dict]] = []
    for r in platform["output"] + platform["void"] + platform["unused"]:
        entries.append(("platform-sales" if r["side"] == "output" else "platform-input", r))
    seen_sales = {plat._dedupe_key(r) for _, r in entries}
    for i, line in enumerate(sales_lines, 1):
        r = plat.parse_record(line)
        if r["side"] != "output":
            raise ValueError(f"sales_lines[{i}] is not a sales record (格式代號 {r['format_code']})")
        if r["seller_ubn"] not in (ubn, None):
            raise ValueError(f"sales_lines[{i}] seller {r['seller_ubn']} is not the client {ubn}")
        if plat._dedupe_key(r) not in seen_sales:
            seen_sales.add(plat._dedupe_key(r))
            entries.append(("sales-import", r))

    skipped: list[dict] = []
    lr: list[dict] = []
    if result is not None:
        recs, skipped = export.media_records(_client_result(result, ubn), {ubn: tax_reg_no}, include_hold=include_hold)
        lr = [plat.parse_record(x) for x in recs.get(ubn, [])]
        for r in lr:
            entries.append(("ledger-room", r))
    lr_keys = {_voucher_key(r): r for r in lr if _voucher_key(r)}
    duplicates = []
    for r in platform["input"]:
        k = _voucher_key(r)
        if k and k in lr_keys:
            mine = lr_keys[k]
            diff = {f: (mine[f], r[f]) for f in ("amount", "tax", "deduction", "format_code", "seller_ubn")
                    if mine[f] != r[f]}
            duplicates.append({"voucher": k[1], "kept": "ledger-room", "dropped": "platform-input", "differs": diff})
            continue
        entries.append(("platform-input", r))

    overrides = dict(deduction_overrides or {})
    built = []
    for src, r in entries:
        rec = r["raw"]
        if rec[2:11] != tax_reg_no and src.startswith("platform"):
            notes.append(f"{src} line {r.get('line')}: 稅籍編號 {rec[2:11]} replaced by {tax_reg_no}")
        rec = rec[:2] + tax_reg_no + rec[11:]
        k = _voucher_key(r)
        if k and k[1] in overrides:
            code = str(overrides.pop(k[1]))
            if r["side"] != "input" or code not in ("1", "2", "3", "4"):
                raise ValueError(f"deduction override {k[1]} -> {code!r}: needs an input line and a code 1-4")
            rec = rec[:72] + code + rec[73:]
        built.append((src, rec))
    if overrides:
        notes.append("deduction overrides for vouchers not in the file: " + ", ".join(sorted(overrides)))

    def order(item):
        rec = item[1]
        return (rec[0:2], rec[18:23], rec[39:49], rec[35:49])
    built.sort(key=order)
    lines, sources, records = [], [], []
    for n, (src, rec) in enumerate(built, 1):
        rec = rec[:11] + f"{n:07d}" + rec[18:]
        r = plat.parse_record(rec)             # every final line must parse as a 附件五 record
        lines.append(rec)
        sources.append(src)
        records.append(r)
    if not lines:
        notes.append("no records: nothing to file for this period (an empty file is not a valid media file)")
    return MediaFile(ubn, tax_reg_no, lines, sources, records, duplicates, skipped, notes)


def write_media_file(mf: MediaFile, out_dir: Path, newline: str = "\r\n") -> Path:
    """<UBN>.TXT, ASCII (第十七點), one record per line. CRLF is my choice; the regulation does not name the separator."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / mf.filename()
    p.write_bytes("".join(x + newline for x in mf.lines).encode("ascii"))
    return p


# ---------------------------------------------------------------------------------------------------- return totals

_SALES_TAXABLE = {"31": (1, 2), "35": (5, 6), "32": (9, 10), "36": (13, 14), "33": (17, 18), "34": (17, 18)}
_SALES_EXEMPT = {"31": 4, "35": 8, "32": 12, "36": 16, "33": 20, "34": 20}
_INPUT_BOX = {"21": (28, 29, 30, 31), "26": (28, 29, 30, 31), "25": (32, 33, 34, 35), "22": (36, 37, 38, 39),
              "27": (36, 37, 38, 39), "28": (78, 79, 80, 81), "23": (40, 41, 42, 43), "24": (40, 41, 42, 43),
              "29": (40, 41, 42, 43)}
_INPUT_DEDUCTIONS = {"23", "24", "29"}


class _Box:
    """Amount / tax of one box pair: exclusive lines add as they are; tax-inclusive lines are totalled and split once
    (第二十點(十五)1(1)(3))."""

    def __init__(self):
        self.amount = 0
        self.tax = 0
        self.inclusive = 0

    def add(self, r: dict):
        if r["tax_inclusive"]:
            self.inclusive += r["amount"]
        else:
            self.amount += r["amount"]
            self.tax += r["tax"]

    def result(self) -> tuple[int, int]:
        a, t = split_inclusive(self.inclusive) if self.inclusive else (0, 0)
        return self.amount + a, self.tax + t


def _invoice_count(r: dict) -> int:
    if r["agg_flag"] == "A" and r["invoice_no"] and r["invoice_end_no"]:
        return int(r["invoice_end_no"]) - int(r["invoice_no"][2:]) + 1
    return 1


def nondeductible_ratio(taxable_and_zero: int, exempt: int, special: int = 0, land: int = 0) -> tuple[int, str | None]:
    """不得扣抵比例 in whole percent ([表] 403 填寫說明(十三)): (④+⑤-⑧) / (⑦-⑧), ⑦ = ①+③+④+⑤, fraction dropped;
    ④+⑤-⑧ < 0 counts as 0 and ①+③ < 0 counts as 0. Returns (percent, note)."""
    num = max(exempt + special - land, 0)
    den = max(taxable_and_zero, 0) + exempt + special - land
    if den <= 0:
        return 0, "no net sales after land: 不得扣抵比例 set to 0 (the form does not cover this case)"
    return (num * 100) // den, None


def annual_adjustment(year_input_tax: int, year_s19_tax: int, year_deducted_tax: int, year_ratio_pct: int) -> int:
    """調整稅額 for the last period of the year ([兼營] Art. 7(1)):
    已扣抵之進項稅額 - (進項稅額 - 第19條第1項不得扣抵之進項稅額) x (1 - 當年度不得扣抵比例), rounded half up.
    Positive: tax to pay (pass as year_end_adjustment -> box 105); negative: refund (box 109)."""
    allowed = Fraction(year_input_tax - year_s19_tax) * (100 - year_ratio_pct) / 100
    return year_deducted_tax - _half_up(allowed)


def return_totals(records: list[dict], *, form: str = "401", period=None, prior_credit: int = 0,
                  land_sales: int = 0, fixed_asset_sales: int = 0, plain_receipts: int = 0,
                  plain_receipts_fixed_assets: int = 0, year_end_adjustment: int | None = None,
                  invoices_used: int | None = None) -> dict:
    """Box totals (代號 -> NT$) of form 401 or 403 from the parsed media-file records of one client and period.

    prior_credit: 上期累積留抵稅額 (108) = last return's 115. land_sales / fixed_asset_sales: 26 / 27 (land is exempt,
    so a 401 client cannot have it). plain_receipts: small-scale businesses' plain receipts for 48 ([表] 401 (十一)).
    year_end_adjustment: annual_adjustment() for a 403 client's last period of the year. invoices_used: field 8
    override (default: count of issued 31 / 32 / 35 invoices, voided ones excluded)."""
    if form == "404":
        raise NotSupported("form 404 is not supported")
    if form not in ("401", "403"):
        raise ValueError(f"form must be 401 or 403, got {form!r}")
    notes: list[str] = []
    sales = {k: _Box() for k in (1, 5, 9, 13, 17)}
    zero = {7: 0, 15: 0, 19: 0}
    exempt = {4: 0, 8: 0, 12: 0, 16: 0, 20: 0}
    ded = {k: _Box() for k in (28, 30, 32, 34, 36, 38, 78, 80, 40, 42)}
    nonded = {"goods": _Box(), "fixed": _Box(), "goods_back": _Box(), "fixed_back": _Box()}
    used = 0
    for r in records:
        if r["status"] in ("void", "unused"):
            continue
        fmt = r["format_code"]
        if r["side"] == "output":
            if fmt in ("37", "38") or r["special_rate"]:
                raise NotSupported(f"special-rate (特種稅額) sales line {r['raw'][11:18]}: form 403 with special "
                                   "tax rates and form 404 are not supported")
            if fmt in ("31", "32", "35") and not r["is_allowance"]:
                used += _invoice_count(r)
            tt = r["tax_type"]
            if tt == "1":
                sales[_SALES_TAXABLE[fmt][0]].add(r)
            elif tt == "2":
                if fmt in ("33", "34"):
                    zero[19] += r["amount"]
                elif r["customs_flag"] == "1":
                    zero[7] += r["amount"]
                elif r["customs_flag"] == "2":
                    zero[15] += r["amount"]
                else:
                    raise ValueError(f"zero-rated sales line {r['raw'][11:18]} has no 通關方式註記 (1 or 2)")
            elif tt == "3":
                exempt[_SALES_EXEMPT[fmt]] += r["amount"]
            continue
        code = r["deduction"]
        if code not in ("1", "2", "3", "4"):
            raise ValueError(f"input line {r['raw'][11:18]} has 扣抵代號 {code!r}, needs 1-4")
        fixed = code in ("2", "4")
        back = fmt in _INPUT_DEDUCTIONS
        if code in ("1", "2"):
            g_amt, _g_tax, f_amt, _f_tax = _INPUT_BOX[fmt]
            ded[f_amt if fixed else g_amt].add(r)
        else:
            nonded[("fixed" if fixed else "goods") + ("_back" if back else "")].add(r)

    b: dict[int, int] = {}
    for amt_code, box in sales.items():
        b[amt_code], b[amt_code + 1] = box.result()
    b.update(zero)
    b.update(exempt)
    b[21] = b[1] + b[5] + b[9] + b[13] - b[17]
    b[22] = b[2] + b[6] + b[10] + b[14] - b[18]
    b[23] = b[7] + b[15] - b[19]
    b[24] = b[4] + b[8] + b[12] + b[16] - b[20]
    if form == "401" and (b[24] or any(exempt.values()) or land_sales):
        raise ValueError("exempt sales (or land sales) in the period: this client files form 403, not 401 "
                         "([表] 401 說明二)")
    b[25] = b[21] + b[23] + (b[24] if form == "403" else 0)
    b[26] = land_sales
    b[27] = fixed_asset_sales
    for amt_code, box in ded.items():
        b[amt_code], b[amt_code + 1] = box.result()
    b[44] = b[28] + b[32] + b[36] + b[78] - b[40]
    b[45] = b[29] + b[33] + b[37] + b[79] - b[41]
    b[46] = b[30] + b[34] + b[38] + b[80] - b[42]
    b[47] = b[31] + b[35] + b[39] + b[81] - b[43]
    nd = {k: v.result()[0] for k, v in nonded.items()}
    b[48] = b[44] + nd["goods"] - nd["goods_back"] + plain_receipts
    b[49] = b[46] + nd["fixed"] - nd["fixed_back"] + plain_receipts_fixed_assets
    b[73] = 0
    b[74] = b[75] = b[76] = 0
    b[82] = 0
    b[101] = b[22]
    b[103] = 0
    b[104] = 0
    b[105] = b[109] = 0
    ratio = None
    if form == "403":
        ratio, note = nondeductible_ratio(b[21] + b[23], b[24], 0, land_sales)
        if note:
            notes.append(note)
        b[50] = ratio
        b[51] = _half_up(Fraction(b[45] + b[47]) * (100 - ratio) / 100)
        b[107] = b[51]
        months = plat.period_months(period) if period is not None else None
        if year_end_adjustment is not None:
            if year_end_adjustment > 0:
                b[105] = year_end_adjustment
            else:
                b[109] = -year_end_adjustment
        elif months and months[-1].endswith("-12"):
            notes.append("last period of the year: the year-end adjustment (兼營營業人營業稅額計算辦法 Art. 7) is "
                         "not included; compute it with annual_adjustment() and pass year_end_adjustment (105 / 109)")
    else:
        b[107] = b[45] + b[47]
    b[106] = b[101] + b[103] + b[104] + b[105]
    b[108] = prior_credit
    b[110] = b[107] + b[108] + b[109]
    b[111] = max(b[106] - b[110], 0)
    b[112] = max(b[110] - b[106], 0)
    b[113] = _half_up(Fraction(b[23]) * 5 / 100) + b[47]
    b[114] = (b[113] if b[112] > b[113] else b[112]) if b[112] > 0 else 0
    b[115] = b[112] - b[114]
    return {"form": form, "boxes": dict(sorted(b.items())), "nondeductible_ratio_pct": ratio,
            "invoices_used": used if invoices_used is None else invoices_used, "notes": notes}


# ---------------------------------------------------------------------------------------------------- TET_U

# 附件六, field by field: (field no, box code or header name, picture, width). S = signed (sign in the last digit).
_NUM_FIELDS: list[tuple[int, int, str, int]] = [
        (9, 1, "S", 12), (10, 5, "S", 12), (11, 9, "S", 12), (12, 13, "S", 12), (13, 17, "S", 12), (14, 21, "S", 12),
        (15, 2, "S", 10), (16, 6, "S", 10), (17, 10, "S", 10), (18, 14, "S", 10), (19, 18, "S", 10), (20, 22, "S", 10),
        (21, 82, "S", 12), (22, 7, "S", 12), (23, 15, "S", 12), (24, 19, "S", 12), (25, 23, "S", 12),
        (26, 4, "S", 12), (27, 8, "S", 12), (28, 12, "S", 12), (29, 16, "S", 12), (30, 20, "S", 12), (31, 24, "S", 12),
        (32, 52, "S", 12), (33, 53, "S", 10), (34, 54, "S", 12), (35, 55, "S", 10), (36, 56, "S", 12), (37, 57, "S", 10),
        (38, 58, "S", 12), (39, 59, "S", 10), (40, 60, "S", 12), (41, 61, "S", 10), (42, 62, "S", 12),
        (43, 63, "S", 12), (44, 64, "S", 10), (45, 65, "S", 12), (46, 66, "S", 10),
        (47, 25, "S", 12), (48, 26, "S", 12), (49, 27, "S", 12),
        (50, 28, "S", 12), (51, 30, "S", 12), (52, 32, "S", 12), (53, 34, "S", 12), (54, 36, "S", 12), (55, 38, "S", 12),
        (56, 40, "S", 12), (57, 42, "S", 12), (58, 44, "S", 12), (59, 46, "S", 12),
        (60, 29, "S", 10), (61, 31, "S", 10), (62, 33, "S", 10), (63, 35, "S", 10), (64, 37, "S", 10), (65, 39, "S", 10),
        (66, 41, "S", 10), (67, 43, "S", 10), (68, 45, "S", 10), (69, 47, "S", 10),
        (70, 48, "S", 12), (71, 49, "S", 12), (72, 50, "9", 3), (73, 51, "S", 10),
        (74, 78, "S", 12), (75, 80, "S", 12), (76, 73, "S", 12), (77, 74, "S", 12),
        (78, 79, "S", 10), (79, 81, "S", 10), (80, 75, "S", 10), (81, 76, "S", 10),
        (82, 101, "S", 10), (83, 103, "S", 10), (84, 104, "S", 10), (85, 105, "S", 10), (86, 106, "S", 10),
        (87, 107, "S", 10), (88, 108, "S", 10), (89, 109, "S", 10), (90, 110, "S", 10), (91, 111, "S", 10),
        (92, 112, "S", 10), (93, 113, "S", 10), (94, 114, "S", 10), (95, 115, "S", 10),
        (105, 67, "S", 12), (106, 69, "S", 12), (107, 71, "S", 12), (108, 68, "S", 10), (109, 70, "S", 10),
        (110, 72, "S", 10), (111, 84, "S", 12), (112, 85, "S", 10)]
_POS = "{ABCDEFGHI"
_NEG = "}JKLMNOPQR"


def signed(v: int, width: int) -> str:
    """S9(width): zero-filled, sign carried in the last digit (第二十一點(十八))."""
    digits = str(abs(int(v))).zfill(width)
    if len(digits) > width:
        raise ValueError(f"{v} does not fit S9({width})")
    return digits[:-1] + (_NEG if v < 0 else _POS)[int(digits[-1])]


def unsigned(v: int, width: int) -> str:
    if v is None or int(v) < 0:
        raise ValueError(f"9({width}) needs a non-negative integer, got {v!r}")
    s = str(int(v)).zfill(width)
    if len(s) > width:
        raise ValueError(f"{v} does not fit 9({width})")
    return s


def _x(v, width: int, chinese: bool = False) -> str:
    v = (v or "").strip()
    if len(v) > width:
        raise ValueError(f"{v!r} longer than {'C' if chinese else 'X'}({width})")
    if not chinese and not v.isascii():
        raise ValueError(f"{v!r}: X fields take no Chinese or full-width characters")
    if "|" in v or "\r" in v or "\n" in v:
        raise ValueError(f"{v!r}: no '|' or line breaks inside a field")
    return v


def tet_u_fields(totals: dict, *, ubn: str, tax_reg_no: str, period_end: str, filing_kind: str = "1",
                 county: str, self_or_agent: str = "2", filer_name: str = "", filer_id: str = "",
                 phone_area: str = "", phone: str = "", phone_ext: str = "", agent_reg_no: str = "",
                 total_pay_code: str = "0", file_no: str = "00000000") -> list[str]:
    """The 112 field values of the 申報書 record (附件六). period_end: the period's last month, "YYYY-MM"."""
    if not re.fullmatch(r"\d{8}", ubn):
        raise ValueError(f"UBN must be 8 digits, got {ubn!r}")
    if not re.fullmatch(r"\d{9}", tax_reg_no or ""):
        raise ValueError(f"稅籍編號 must be 9 digits, got {tax_reg_no!r}")
    if county not in COUNTY_CODES:
        raise ValueError(f"縣市別 {county!r} is not an 附件七 code")
    if filing_kind not in ("1", "2") or self_or_agent not in ("1", "2") or total_pay_code not in ("0", "1", "2"):
        raise ValueError("申報種類 1/2, 自行或委任 1/2, 總繳代號 0/1/2")
    if self_or_agent == "2" and not agent_reg_no:
        raise ValueError("委任申報 needs the agent's 登錄（文）字號 (第二十一點(十六))")
    if not re.fullmatch(r"[0-9A-Za-z]{8}", file_no):
        raise ValueError("檔案編號 must be 8 letters or digits")
    data_kind = {"401": "1", "403": "3"}[totals["form"]]
    y, m = period_end.split("-")
    f = [""] * (TET_U_FIELDS + 1)
    f[1], f[2], f[3] = data_kind, file_no, ubn
    f[4] = f"{int(y) - 1911:03d}{int(m):02d}"
    f[5] = "5" if total_pay_code == "1" else "1"
    f[6], f[7] = tax_reg_no, total_pay_code
    f[8] = unsigned(totals["invoices_used"], 10)
    boxes = totals["boxes"]
    for no, code, pic, w in _NUM_FIELDS:
        v = boxes.get(code, 0) or 0
        f[no] = unsigned(v, w) if pic == "9" else signed(v, w)
    f[96], f[97], f[98] = filing_kind, county, self_or_agent
    f[99] = _x(filer_id, 10)
    f[100] = _x(filer_name, 12, chinese=True)
    f[101], f[102], f[103] = _x(phone_area, 4), _x(phone, 11), _x(phone_ext, 5)
    f[104] = _x(agent_reg_no, 50, chinese=True) if self_or_agent == "2" else ""
    return f[1:]


def tet_u_line(totals: dict, **header) -> str:
    vals = tet_u_fields(totals, **header)
    assert len(vals) == TET_U_FIELDS
    return "|".join(vals)


def write_tet_u(line: str, out_dir: Path, ubn: str) -> Path:
    """<UBN>.TET_U: UTF-8 without BOM, the record followed by CRLF (附件六 說明(1)(3))."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{ubn}.TET_U"
    p.write_bytes((line + "\r\n").encode("utf-8"))
    return p


def file_return(client: dict, period, out_dir: Path, *, tax_reg_no: str, header: dict, result: dict | None = None,
                platform: dict | None = None, sales_lines=(), deduction_overrides: dict | None = None,
                **totals_kw) -> dict:
    """Write <UBN>.TXT, <UBN>.TET_U and <UBN>_totals.json for one client and period. header: the TET_U filer fields
    (county, self_or_agent, agent_reg_no, filer_name, ...). Returns the paths, totals and everything left out.
    The files are drafts until the MOF checking program accepts them."""
    form = client_form(client)
    months = plat.period_months(period if period is not None else client.get("period"))
    mf = assemble_media_file(client, period, tax_reg_no=tax_reg_no, result=result, platform=platform,
                             sales_lines=sales_lines, deduction_overrides=deduction_overrides)
    totals = return_totals(mf.records, form=form, period=period, **totals_kw)
    out_dir = Path(out_dir)
    txt = write_media_file(mf, out_dir)
    line = tet_u_line(totals, ubn=mf.client_ubn, tax_reg_no=tax_reg_no, period_end=months[-1], **header)
    tet = write_tet_u(line, out_dir, mf.client_ubn)
    summary = {
        "draft": True,
        "status": "not run through the MOF checking program; not filed",
        "client_ubn": mf.client_ubn, "form": form, "period": [months[0], months[-1]],
        "records": len(mf.lines), "by_source": {s: mf.sources.count(s) for s in sorted(set(mf.sources))},
        "boxes": {str(k): v for k, v in totals["boxes"].items()},
        "nondeductible_ratio_pct": totals["nondeductible_ratio_pct"], "invoices_used": totals["invoices_used"],
        "notes": mf.notes + totals["notes"], "duplicates": mf.duplicates, "skipped": mf.skipped,
        "sources": [plat.SOURCE, "附件六 https://law-out.mof.gov.tw/Download.ashx?FileID=51326&id=GL009478&type=LAW"],
    }
    js = out_dir / f"{mf.client_ubn}_totals.json"
    js.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"txt": txt, "tet_u": tet, "json": js, "totals": totals, "media": mf, "summary": summary}
