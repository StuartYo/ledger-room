"""Import the MOF e-invoice platform CSV export (載具 / 消費明細 download): structured data, no OCR.

Most of a firm's input vouchers already exist as e-invoices on the MOF platform. Reading those rows directly is
faster and exact, and the same rule engine runs on them. The export has one row per line item:

  載具自訂名稱, 發票日期 (YYYYMMDD), 發票號碼, 發票金額, 發票狀態, 折讓, 賣方統一編號, 賣方名稱, 賣方地址, 買方統編,
  消費明細_數量, 消費明細_單價, 消費明細_金額, 消費明細_品名

and ends with platform footnote lines. This module detects the encoding (UTF-8 or Big5), finds the header by name,
skips footnotes, groups line items into invoices, and keeps discount (negative) and zero-price bundle lines as they
are. Voided invoices (發票狀態 contains 作廢) are kept out of the documents handed to the rules.

The 發票金額 column is not always the invoice total: in exports seen so far it repeats each row's line amount.
So the invoice amount is the sum of the line amounts when every row's 發票金額 equals its own line amount, the
repeated value when it is the same on every row, and otherwise the line sum (counted as amount_conflicts).
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import rules

COLS = {"date": "發票日期", "no": "發票號碼", "amount": "發票金額", "status": "發票狀態", "allowance": "折讓",
        "seller_ubn": "賣方統一編號", "seller_name": "賣方名稱", "buyer_ubn": "買方統編",
        "qty": "消費明細_數量", "unit": "消費明細_單價", "line": "消費明細_金額", "item": "消費明細_品名"}
_NUM = re.compile(r"^-?\d+(\.\d+)?$")


@dataclass
class Line:
    desc: str
    qty: float | None
    unit: float | None
    amount: float | None


@dataclass
class EInvoice:
    invoice_no: str
    date: date
    amount: float | None
    status: str
    allowance: str
    seller_ubn: str | None
    seller_name: str
    buyer_ubn: str | None
    lines: list[Line] = field(default_factory=list)
    row_amounts: list[float | None] = field(default_factory=list)
    amount_basis: str = ""          # per-line | invoice | conflict

    def settle(self) -> None:
        line_amts = [l.amount for l in self.lines]
        if self.row_amounts and all(a is not None and b is not None and abs(a - b) < 0.005
                                    for a, b in zip(self.row_amounts, line_amts)):
            self.amount, self.amount_basis = self.line_sum, "per-line"
        elif self.row_amounts and len(set(self.row_amounts)) == 1 and self.row_amounts[0] is not None:
            self.amount, self.amount_basis = self.row_amounts[0], "invoice"
        else:
            self.amount, self.amount_basis = self.line_sum, "conflict"

    @property
    def voided(self) -> bool:
        return "作廢" in self.status

    @property
    def line_sum(self) -> float:
        return round(sum(l.amount or 0 for l in self.lines), 2)

    @property
    def discount_lines(self) -> int:
        return sum(1 for l in self.lines if (l.amount or 0) < 0)

    @property
    def zero_lines(self) -> int:
        return sum(1 for l in self.lines if (l.amount or 0) == 0)


def _num(s: str) -> float | None:
    s = (s or "").replace(",", "").strip()
    return float(s) if _NUM.match(s) else None


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp950", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("not UTF-8 or Big5")


def parse(text: str) -> tuple[list[EInvoice], dict]:
    """Returns (invoices in file order, counters)."""
    reader = csv.reader(io.StringIO(text))
    idx = None
    by_key: dict[tuple, EInvoice] = {}
    stats = {"rows": 0, "line_items": 0, "skipped_rows": 0}
    for row in reader:
        cells = [c.strip() for c in row]
        if idx is None:
            if COLS["no"] in cells and COLS["date"] in cells:
                idx = {k: cells.index(v) for k, v in COLS.items() if v in cells}
            continue
        stats["rows"] += 1
        get = lambda k: cells[idx[k]] if k in idx and idx[k] < len(cells) else ""   # noqa: E731
        d, no = get("date"), re.sub(r"[^A-Za-z0-9]", "", get("no")).upper()
        if not (len(d) == 8 and d.isdigit() and re.fullmatch(r"[A-Z]{2}\d{8}", no)):
            stats["skipped_rows"] += 1          # platform footnotes and blank lines
            continue
        key = (no, d)
        inv = by_key.get(key)
        if inv is None:
            s_ubn, b_ubn = get("seller_ubn"), get("buyer_ubn")
            inv = by_key[key] = EInvoice(
                invoice_no=no, date=date(int(d[:4]), int(d[4:6]), int(d[6:])), amount=_num(get("amount")),
                status=get("status"), allowance=get("allowance"),
                seller_ubn=s_ubn if re.fullmatch(r"\d{8}", s_ubn) else None, seller_name=get("seller_name"),
                buyer_ubn=b_ubn if re.fullmatch(r"\d{8}", b_ubn) else None)
        if get("item") or get("line"):
            inv.lines.append(Line(get("item"), _num(get("qty")), _num(get("unit")), _num(get("line"))))
            inv.row_amounts.append(_num(get("amount")))
            stats["line_items"] += 1
    if idx is None:
        raise ValueError("no header row with 發票號碼 and 發票日期")
    invs = list(by_key.values())
    for inv in invs:
        inv.settle()
    stats["amount_conflicts"] = sum(1 for i in invs if i.amount_basis == "conflict")
    return invs, stats


def read_platform_csv(path: str | Path) -> tuple[list[EInvoice], dict]:
    return parse(decode(Path(path).read_bytes()))


def to_rule_doc(inv: EInvoice, client_id: str, buyer_ubn: str | None = None) -> rules.Doc:
    """An e-invoice as the rule engine sees it. Fields come from the platform, so nothing is uncertain.
    buyer_ubn overrides the printed buyer (used to ask 'what if this had been issued to the client company')."""
    total = round(inv.amount) if inv.amount is not None else None
    b = buyer_ubn or inv.buyer_ubn
    return rules.Doc(
        doc_id=f"{client_id}/{inv.date:%Y-%m}/{inv.invoice_no}", client_id=client_id, month=f"{inv.date:%Y-%m}",
        doc_type="e_invoice", invoice_no=inv.invoice_no, date=inv.date, seller_name=inv.seller_name,
        seller_ubn=inv.seller_ubn, buyer_ubn=b,
        items=[rules.Item(l.desc, l.qty, round(l.amount) if l.amount is not None else None) for l in inv.lines],
        total=total, tax=(total - round(total / 1.05)) if (b and total) else None)
