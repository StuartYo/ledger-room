"""Read the e-invoice platform's media filing download (媒體申報檔下載) for a client and period.

What a firm gets from the MOF e-invoice platform (電子發票整合服務平台), menu 查詢與下載 > 媒體申報檔下載
(包含進項、銷項): one text file per business, named <UBN>.txt, in the same 81-character record layout as the VAT media
filing file (營業人進、銷項資料檔). The platform builds it per filing period or month at 00/04/08/12/15/18/21 h, and a
bookkeeper the client has authorised (專業代理人) may download it too.
  Source: 財政部財政資訊中心《電子發票整合服務平台 營業人導入與操作說明》pp. 160-167 (PDF has no date)
  https://www.einvoice.nat.gov.tw/static/ptl/ein_upload/attachments/1500544126963_0.pdf
  p. 167 shows the file open in Notepad: 81-character lines, format 31 sales lines, 課稅別 F for voided invoices.

Record layout (positions, codes, field meanings) from 營業稅電子資料申報繳稅作業要點 第二十點 and 附件五, amended
民國113年04月12日 (台財資字第1130001073號令), https://law-out.mof.gov.tw/LawContent.aspx?id=GL009478 , read 2026-11-07.
The parser is generic: it reads any 81-character input (21-29) or output (31-38) record, so the full media file
(ledger_room/filing.py) reuses it for Ledger Room's own lines.

Status of a record:
  void       課稅別 F (作廢發票): kept out of the invoice list, kept for the filing (the file must carry it)
  unused     課稅別 D (空白未使用之統一發票): same
  allowance  格式代號 23 / 24 (進貨退出或折讓) and 33 / 34 (銷貨退回或折讓): kept, with their own format code
  issued     everything else

Not verified (read this before relying on it):
  - The platform download is read as the 附件五 layout because the manual's screenshot shows that layout; no real
    download has been parsed (logging in needs the client's credentials). A real file may carry codes the screenshot
    does not show (for example input lines of formats 26 / 27, or a different 稅籍編號 than the firm's records).
  - Whether the platform's input lines carry the platform's own guess of 扣抵代號 (probably 1): the media file has no
    item names, so the VAT Act Art. 19 keyword table cannot run on these lines; filing.py lets the bookkeeper override
    the code per invoice.
  - The platform's 進項發票查詢與列印 Excel download (發票主檔 / 發票明細 sheets) is NOT read: its column names are not
    published anywhere I could reach without logging in. Only the media-file path is implemented.
  - The module name shadows the standard library's `platform` only for code run from inside ledger_room/ as a script
    folder; `import platform` elsewhere (pipeline.py) still gets the standard library.

Where files go by default: data/platform/<client UBN>/**/*.txt (any sub-folder, e.g. one per period). Repeated
downloads of the same period are de-duplicated record by record.
"""
from __future__ import annotations

import re
from pathlib import Path

RECORD_LEN = 81
ROOT = Path(__file__).resolve().parents[1]
PLATFORM_ROOT = ROOT / "data" / "platform"
SOURCE = ("營業稅電子資料申報繳稅作業要點 第二十點及附件五（民國113年04月12日修正）"
          " https://law-out.mof.gov.tw/LawContent.aspx?id=GL009478 ；電子發票整合服務平台 營業人導入與操作說明 第160-167頁"
          " https://www.einvoice.nat.gov.tw/static/ptl/ein_upload/attachments/1500544126963_0.pdf")

INPUT_FORMATS = {str(n) for n in range(21, 30)}
OUTPUT_FORMATS = {str(n) for n in range(31, 39)}
ALLOWANCE_FORMATS = {"23", "24", "33", "34", "38"}
FORMAT_LABEL = {   # 第二十點(一)
    "21": "進項三聯式、電子計算機統一發票",
    "22": "進項二聯式收銀機統一發票、載有稅額之其他憑證",
    "23": "三聯式等進貨退出或折讓證明單",
    "24": "二聯式收銀機統一發票、載有稅額之其他憑證之進貨退出或折讓證明單",
    "25": "進項三聯式收銀機統一發票、一般稅額計算之電子發票",
    "26": "彙總登錄每張稅額五百元以下之進項三聯式、電子計算機統一發票",
    "27": "彙總登錄每張稅額五百元以下之進項二聯式收銀機統一發票、載有稅額之其他憑證",
    "28": "進項海關代徵營業稅繳納證",
    "29": "進項海關退還溢繳營業稅申報單",
    "31": "銷項三聯式統一發票",
    "32": "銷項二聯式、二聯式收銀機統一發票",
    "33": "三聯式等銷貨退回或折讓證明單",
    "34": "二聯式等銷貨退回或折讓證明單、免用發票銷貨退回或折讓",
    "35": "銷項三聯式收銀機統一發票、一般稅額計算之電子發票",
    "36": "銷項免用統一發票",
    "37": "銷項憑證、特種稅額計算之電子發票",
    "38": "特種稅額計算之銷貨退回或折讓證明單",
}
TAX_TYPE_LABEL = {"1": "應稅", "2": "零稅率", "3": "免稅", "F": "作廢", "D": "空白未使用"}
_UNIFORM_NO = re.compile(r"^[A-Z]{2}\d{8}$")


def _f(rec: str, a: int, b: int) -> str:
    return rec[a - 1:b]


def _int(s: str) -> int | None:
    s = s.strip()
    return int(s) if s.isdigit() else None


def parse_record(rec: str) -> dict:
    """One 81-character record -> normalized dict. Raises ValueError when the line cannot be a 附件五 record."""
    rec = rec.rstrip("\r\n")
    if len(rec) != RECORD_LEN:
        raise ValueError(f"record length {len(rec)}, must be {RECORD_LEN}")
    if not rec.isascii():
        raise ValueError("non-ASCII record (第十七點: ASCII)")
    fmt = _f(rec, 1, 2)
    if fmt not in INPUT_FORMATS | OUTPUT_FORMATS:
        raise ValueError(f"格式代號 {fmt!r} is not 21-29 or 31-38")
    side = "input" if fmt in INPUT_FORMATS else "output"
    year, month = _int(_f(rec, 19, 21)), _int(_f(rec, 22, 23))
    if year is None or month is None or not 1 <= month <= 12:
        raise ValueError(f"資料所屬年月 {_f(rec, 19, 23)!r} is not YYYMM")
    amount, tax = _int(_f(rec, 50, 61)), _int(_f(rec, 63, 72))
    if amount is None or tax is None:
        raise ValueError("銷售金額 / 營業稅額 must be digits")
    tax_type, agg = _f(rec, 62, 62), _f(rec, 80, 80)
    r = {
        "source": "platform_media_file", "side": side, "format_code": fmt, "kind": FORMAT_LABEL[fmt],
        "tax_reg_no": _f(rec, 3, 11), "serial": _f(rec, 12, 18),
        "year_roc": year, "month": f"{year + 1911:04d}-{month:02d}",
        "buyer_ubn": None, "seller_ubn": None, "invoice_no": None, "invoice_end_no": None, "other_voucher_no": None,
        "customs_no": None, "count": None,
        "amount": amount, "tax": tax, "tax_type": tax_type, "tax_type_label": TAX_TYPE_LABEL.get(tax_type, "?"),
        "deduction": _f(rec, 73, 73).strip() or None, "special_rate": _f(rec, 79, 79).strip() or None,
        "agg_flag": agg.strip() or None, "customs_flag": _f(rec, 81, 81).strip() or None,
        "raw": rec,
    }
    no = _f(rec, 40, 49)
    if fmt in ("28", "29"):                                   # 32-35 blank, 36-49 customs certificate number
        r["customs_no"] = _f(rec, 36, 49).strip() or None
        r["buyer_ubn"] = _f(rec, 24, 31).strip() or None
    elif side == "output":
        if agg == "A":                                         # aggregated sales: 24-31 is the last invoice number
            r["invoice_end_no"] = _f(rec, 24, 31).strip() or None
        else:
            r["buyer_ubn"] = _f(rec, 24, 31).strip() or None
        r["seller_ubn"] = _f(rec, 32, 39).strip() or None
    else:
        r["buyer_ubn"] = _f(rec, 24, 31).strip() or None
        if agg == "A":                                         # aggregated input: 32-35 count of vouchers
            r["count"] = _int(_f(rec, 32, 35))
        else:
            r["seller_ubn"] = _f(rec, 32, 39).strip() or None
    if fmt not in ("28", "29"):
        if _UNIFORM_NO.match(no):
            r["invoice_no"] = no
        else:
            r["other_voucher_no"] = no.strip() or None
    r["status"] = ("void" if tax_type == "F" else "unused" if tax_type == "D"
                   else "allowance" if fmt in ALLOWANCE_FORMATS else "issued")
    r["is_allowance"] = fmt in ALLOWANCE_FORMATS
    # 第二十點(十五)(十七): the amount already includes tax when the tax field is zero (sales to non-business buyers,
    # two-part cash-register invoices, transport tickets); the return splits the period total, not each record.
    r["tax_inclusive"] = tax_type == "1" and tax == 0 and amount > 0 and fmt not in ("28", "29")
    return r


def parse_media_text(text: str, source: str | None = None) -> tuple[list[dict], list[dict]]:
    """Parse a whole file. Returns (records, errors); a line that is not a record is reported, never dropped quietly."""
    recs, errs = [], []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = parse_record(line)
        except ValueError as e:
            errs.append({"file": source, "line": i, "error": str(e)})
            continue
        r["file"], r["line"] = source, i
        recs.append(r)
    return recs, errs


def read_media_file(path: Path) -> tuple[list[dict], list[dict]]:
    data = Path(path).read_bytes()
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as e:
        return [], [{"file": str(path), "line": 0, "error": f"not ASCII ({e}); 第十七點 requires ASCII"}]
    return parse_media_text(text, str(path))


# ---------------------------------------------------------------- periods

def period_months(period) -> list[str] | None:
    """'YYYY-MM' months of a period. Accepts None (all), 'YYYY-MM', 'YYYY-MM/YYYY-MM', 'YYYY-MM_YYYY-MM', or a pair
    of dates / months such as a client's "period": ["2026-09-01", "2026-10-31"]."""
    if period is None:
        return None
    if isinstance(period, str):
        parts = re.split(r"[/_]", period)
    else:
        parts = [str(p) for p in period]
    if len(parts) == 1:
        parts = parts * 2
    if len(parts) != 2:
        raise ValueError(f"period {period!r}: give one month or a start and an end")
    (y0, m0), (y1, m1) = [tuple(int(x) for x in p[:7].split("-")) for p in parts]
    out, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if not out:
        raise ValueError(f"period {period!r} ends before it starts")
    return out


def _client_ubn(client) -> str:
    ubn = client.get("ubn") if isinstance(client, dict) else client
    if not (isinstance(ubn, str) and re.fullmatch(r"\d{8}", ubn)):
        raise ValueError(f"client needs an 8-digit UBN, got {ubn!r}")
    return ubn


def _dedupe_key(r: dict) -> tuple:
    return r["raw"][0:2] + r["raw"][18:81]          # everything but 稅籍編號 and 流水號 (file-local)


def load_client(client, period=None, *, root: Path | None = None, files=None) -> dict:
    """All platform records for one client (dict with "ubn", or the UBN) in a period.

    files: explicit list of downloaded files; default every *.txt under <root>/<client UBN>/ (root defaults to
    data/platform). Records are kept when their 資料所屬年月 is in the period (all when period is None), and when they
    belong to this client: input lines whose buyer is the client, output lines whose seller is the client.
    Returns {"client_ubn", "months", "files", "input", "output", "void", "unused", "other_ubn", "errors"}; input and
    output hold issued and allowance records; void / unused hold 課稅別 F / D lines (kept for the filing only)."""
    ubn = _client_ubn(client)
    months = period_months(period)
    if files is None:
        base = Path(root or PLATFORM_ROOT) / ubn
        files = sorted(p for p in base.rglob("*") if p.is_file() and p.suffix.lower() == ".txt") if base.exists() else []
    out = {"client_ubn": ubn, "months": months, "files": [str(f) for f in files], "input": [], "output": [],
           "void": [], "unused": [], "other_ubn": [], "errors": []}
    seen = set()
    for f in files:
        recs, errs = read_media_file(Path(f))
        out["errors"] += errs
        for r in recs:
            if months is not None and r["month"] not in months:
                continue
            key = _dedupe_key(r)
            if key in seen:
                continue
            seen.add(key)
            mine = (r["buyer_ubn"] == ubn) if r["side"] == "input" else (r["seller_ubn"] == ubn)
            if r["side"] == "input" and r["format_code"] in ("28", "29") and r["buyer_ubn"] is None:
                mine = True                                    # customs lines may not repeat the importer's UBN
            if r["side"] == "output" and r["agg_flag"] == "A" and r["seller_ubn"] is None:
                mine = True
            if not mine:
                out["other_ubn"].append(r)
                continue
            if r["status"] == "void":
                out["void"].append(r)
            elif r["status"] == "unused":
                out["unused"].append(r)
            else:
                out[r["side"]].append(r)
    return out


def platform_invoices(client, period=None, *, root: Path | None = None, files=None) -> list[dict]:
    """Normalized invoice dicts (input and output) for the client and period: voided and unused numbers excluded,
    allowances (23 / 24 / 33 / 34) kept with status "allowance"."""
    d = load_client(client, period, root=root, files=files)
    return d["input"] + d["output"]


def platform_invoice_numbers(client, period=None, *, root: Path | None = None, files=None) -> set[str]:
    """Uniform-invoice numbers of the client's issued input lines (formats 21 / 22 / 25) in the platform download.

    A photographed voucher with one of these numbers is already on the platform and is not entered again (pipeline
    hook: rules.run_rules(platform_invoices=...)). Allowance lines are left out (their 40-49 number points at the
    original invoice). Voided numbers are left out too: see platform_void_numbers().
    period=None uses every month on file, which also catches a prior-period invoice photographed late."""
    d = load_client(client, period, root=root, files=files)
    return {r["invoice_no"] for r in d["input"]
            if r["format_code"] in ("21", "22", "25") and r["status"] == "issued" and r["invoice_no"]}


def platform_void_numbers(client, period=None, *, root: Path | None = None, files=None) -> set[str]:
    """Invoice numbers the platform file marks 作廢 (課稅別 F) on either side. A photographed voucher with one of these
    numbers must not be claimed (VAT Act Art. 51(1)(5)); the pipeline has no rule for it yet."""
    d = load_client(client, period, root=root, files=files)
    return {r["invoice_no"] for r in d["void"] if r["invoice_no"]}


def platform_numbers_by_client(clients, period=None, *, root: Path | None = None) -> dict[str, set[str]]:
    """{client_id: set} for pipeline.audit(platform_invoices=...); clients is a list or {id: client} of client dicts."""
    items = clients.values() if isinstance(clients, dict) else clients
    return {c["id"]: platform_invoice_numbers(c, period if period is not None else c.get("period"), root=root)
            for c in items}


def roc_month(month: str) -> str:
    """'2026-10' -> '11510'."""
    y, m = month.split("-")
    return f"{int(y) - 1911:03d}{int(m):02d}"
