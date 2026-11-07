"""Accounting-software import file: one row per input voucher, for the firm to map into its accounting software.

Columns (fixed, in this order):
  日期, 發票號碼, 格式代號, 賣方統編, 賣方名稱, 未稅金額, 稅額, 總額, 扣抵代號, 課稅別, 摘要, 狀態, 建議科目

狀態:
  pre-checked          the rules found nothing blocking (READY for the media file, or an expense voucher with only
                       warnings); a bookkeeper still signs off every line
  hold                 a blocking exception, or a voucher that is not this client's (another company's UBN)
  platform-unchecked   a line from the e-invoice platform download: exact numbers, but no item names, so the VAT Act
                       Art. 19 check did not run on it
Photographed vouchers already on the platform (rule IN_PLATFORM) are left out: the platform row stands for them.

建議科目 is left blank unless the voucher matched a keyword in the rules' own tables (rules.S19_LEXICON with keyword
evidence, or rules.EXPENSE_LEXICON taxi / lodging). Codes and names are from the MOEA 商業會計項目表（112年度及以後
適用版本）, 全國商工行政服務入口網 https://gcis.nat.gov.tw/mainNew/subclassNAction.do?method=getFile&pk=182&sub=250
(PDF https://gcis.nat.gov.tw/F/t70492_p, 111.09.28 更新), read 2026-11-07. The mapping assumes a trading or service
business (營業費用 61xx); a manufacturer books factory costs under 51xx. Every value is written "<code> <name>（建議）":
a suggestion, never a decision. Meals are not mapped (伙食費 6128, 交際費 6121 and 旅費 6114 all fit; it depends on
who ate and why).

Accounting software templates (checked 2026-11-07): no publicly documented fixed-column import template was found.
  正航: user-defined column mapping on first import, .xls only (https://www.chi.com.tw/blog/ticket-putin-tax , no date)
  鼎新 A1: imports the platform's own Excel download as is (【發票主檔】 sheet, xlsx, 500 rows a time)
           (https://a1.digiwin.com/noviceNew/novice-content.php?detail=543), not a template of its own
  文中: nothing public found
So this layout needs a column mapping in the software; it has not been imported into any of them (untested).

Output: .xlsx when openpyxl is installed, else CSV in UTF-8 with BOM (Excel opens the Chinese headers).
"""
from __future__ import annotations

import csv
from pathlib import Path

from . import export, rules
from .filing import split_inclusive

COLUMNS = ["日期", "發票號碼", "格式代號", "賣方統編", "賣方名稱", "未稅金額", "稅額", "總額", "扣抵代號", "課稅別",
           "摘要", "狀態", "建議科目"]
ACCOUNT_SOURCE = ("經濟部 商業會計項目表（112年度及以後適用版本）"
                  " https://gcis.nat.gov.tw/mainNew/subclassNAction.do?method=getFile&pk=182&sub=250")
# category -> (code, name), only where the category alone decides the account
ACCOUNT_SUGGESTION = {
    "entertainment": ("6121", "交際費"),
    "employee_benefit": ("6129", "職工福利"),
    "taxi": ("6114", "旅費"),
    "lodging": ("6114", "旅費"),
}
_HOLD_SEVERITIES = {"high", "hold", "review", "medium"}


def _suggest(f: dict, client: dict) -> str:
    d = rules.Doc(doc_id="", client_id="", month="", doc_type=f.get("doc_type"),
                  seller_name=f.get("seller_name"), note=f.get("note"),
                  items=[rules.Item(desc=i.get("desc") or "") for i in f.get("items") or []])
    cat, kw, conf = rules.s19_category(d)
    if not (cat and kw and conf == "high"):
        cat, kw = rules.expense_category(d, client.get("name"))
    if not kw or cat not in ACCOUNT_SUGGESTION:     # keyword evidence only; the model's guess is not enough
        return ""
    code, name = ACCOUNT_SUGGESTION[cat]
    return f"{code} {name}（建議）"


def _summary(f: dict) -> str:
    items = [i.get("desc") for i in f.get("items") or [] if i.get("desc")]
    s = "、".join(items[:3]) + ("…" if len(items) > 3 else "")
    return s or (f.get("note") or "")


def rows(result: dict, platform_records=None) -> list[dict]:
    """Import rows for a run (result.json dict), optionally followed by the platform's input lines
    (platform.load_client(...)["input"]) for the same clients."""
    clients = {c["id"]: c for c in result["clients"]}
    docs = {d["doc_id"]: d for d in result["docs"]}
    ex_by_doc: dict[str, list[dict]] = {}
    for e in result.get("exceptions", []):
        ex_by_doc.setdefault(e["doc_id"], []).append(e)
    out = []
    for r in export.rows(result):
        exs = ex_by_doc.get(r["doc_id"], [])
        if any(e["rule_id"] == "IN_PLATFORM" for e in exs):
            continue
        f = docs[r["doc_id"]]["fields"]
        total = f.get("total")
        tax = r["tax"] if r["tax"] != "" else (f.get("tax") or 0)
        net = f.get("net")
        if net is None and total is not None:
            net = total - (tax or 0)
        blocking = [e for e in exs if e["severity"] in _HOLD_SEVERITIES and e["rule_id"] != "S19_NONDEDUCTIBLE"]
        hold = r["status"] == "HOLD" or (r["status"] == "NOT_IN_FILE" and bool(blocking))
        summary = _summary(f)
        if hold:
            why = r["note"] or ", ".join(sorted({e["rule_id"] for e in blocking}))
            summary = (summary + "；" if summary else "") + f"待處理：{why}"
        out.append({
            "日期": r["date"], "發票號碼": r["invoice_no"], "格式代號": r["format_code_格式代號"],
            "賣方統編": r["seller_ubn"], "賣方名稱": f.get("seller_name") or "",
            "未稅金額": "" if net is None else net, "稅額": tax, "總額": "" if total is None else total,
            "扣抵代號": r["deduction_code_扣抵代號"], "課稅別": r["tax_type_課稅別"], "摘要": summary,
            "狀態": "hold" if hold else "pre-checked",
            "建議科目": _suggest(f, clients[r["client_id"]]),
        })
    for p in platform_records or []:
        if p["side"] != "input" or p["status"] in ("void", "unused"):
            continue
        amount, tax = p["amount"], p["tax"]
        if p["tax_inclusive"]:
            amount, tax = split_inclusive(p["amount"])
        sign = -1 if p["is_allowance"] else 1
        out.append({
            "日期": p["month"], "發票號碼": p["invoice_no"] or p["other_voucher_no"] or p["customs_no"] or "",
            "格式代號": p["format_code"], "賣方統編": p["seller_ubn"] or "", "賣方名稱": "",
            "未稅金額": sign * amount, "稅額": sign * tax, "總額": sign * (amount + tax),
            "扣抵代號": p["deduction"] or "", "課稅別": p["tax_type"],
            "摘要": "電子發票平台媒體申報檔（無品名，未做第19條檢查；日期只有年月）" + ("；折讓" if p["is_allowance"] else ""),
            "狀態": "platform-unchecked", "建議科目": "",
        })
    return out


def write(result: dict, path: Path, platform_records=None, *, force_csv: bool = False) -> Path:
    """Write .xlsx (openpyxl) or, without openpyxl or with force_csv, .csv in UTF-8 with BOM. The suffix of path is
    replaced to match what was written; the written path is returned."""
    data = rows(result, platform_records)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if force_csv:
            raise ImportError
        from openpyxl import Workbook
    except ImportError:
        p = path.with_suffix(".csv")
        with p.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(export.safe_rows(data))
        return p
    p = path.with_suffix(".xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "進項憑證"
    ws.append(COLUMNS)
    for r in data:
        ws.append([r[c] for c in COLUMNS])
        for cell in ws[ws.max_row]:        # receipt text starting with "=" is stored as text, never as a formula
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"
    for col in ("B", "D", "C", "I", "J"):        # numbers that must stay text (leading zeros)
        for cell in ws[col][1:]:
            cell.number_format = "@"
    wb.save(p)
    return p
