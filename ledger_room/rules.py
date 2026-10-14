"""Deterministic audit rules. The model only reads documents; every decision here is code.

Each rule has an id, a severity and an official citation. Rule ids are what the
evaluation counts.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from . import ubn as ubnlib
from .normalize import name_sim

LAW = "https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode="

RULES: dict[str, dict] = {
    "UBN_CHECKSUM": {
        "title": "Invalid Unified Business Number (統一編號) checksum",
        "severity": "high",
        "citations": [
            {"label": "MOF Fiscal Information Agency: UBN check-digit logic revision (mod 10 -> mod 5), 110-12-22",
             "url": ubnlib.RULE_URL},
        ],
    },
    "DUP_CROSS_MONTH": {
        "title": "Duplicate voucher: same client already submitted this document",
        "severity": "high",
        "citations": [
            {"label": "VAT Act Art. 51(1)(5): over-claimed input tax is penalised up to 5x",
             "url": LAW + "G0340080&flno=51"},
        ],
    },
    "DUP_CROSS_CLIENT": {
        "title": "Duplicate voucher: the same document appears under two different clients",
        "severity": "high",
        "citations": [
            {"label": "VAT Act Art. 51(1)(5): over-claimed input tax is penalised up to 5x",
             "url": LAW + "G0340080&flno=51"},
            {"label": "VAT Act Art. 33: deductible vouchers must carry the buyer's name, address and UBN",
             "url": LAW + "G0340080&flno=33"},
        ],
    },
    "S19_NONDEDUCTIBLE": {
        "title": "Input tax not deductible under VAT Act Art. 19",
        "severity": "medium",
        "citations": [
            {"label": "VAT Act Art. 19(1): non-deductible input tax (non-business use, entertainment, "
                      "employee personal benefits, passenger cars for own use)",
             "url": LAW + "G0340080&flno=19"},
        ],
    },
    "OVER_LIMIT": {
        "title": "Exceeds the client's expense policy limit",
        "severity": "medium",
        "citations": [
            {"label": "Client expense policy (configured per client in clients.json)", "url": None},
        ],
    },
    "PERIOD_MISMATCH": {
        "title": "Document date does not match the filing period",
        "severity": "medium",
        "citations": [
            {"label": "VAT Act Art. 35: VAT is filed per two-month period",
             "url": LAW + "G0340080&flno=35"},
            {"label": "VAT Enforcement Rules Art. 29: input tax not claimed in its period may be claimed in the next; "
                      "after that, with a stated reason, for up to ten years",
             "url": LAW + "G0340081&flno=29"},
        ],
    },
}

# Duplicates are cited by what the voucher actually carries. A voucher with input tax that this client would deduct
# risks over-claimed VAT (Art. 51). A voucher without input tax (e.g. a taxi receipt) cannot over-claim VAT; there the
# risk is the same expense being counted twice for income tax.
CITE_DUP_VAT = [
    {"label": "VAT Act Art. 51(1)(5): over-claimed input tax is penalised up to 5x", "url": LAW + "G0340080&flno=51"},
]
CITE_DUP_VAT_CROSS = CITE_DUP_VAT + [
    {"label": "VAT Act Art. 33: deductible vouchers must carry the buyer's name, address and UBN",
     "url": LAW + "G0340080&flno=33"},
]
CITE_DUP_INCOME = [
    {"label": "Income Tax Act Art. 24: taxable income is revenue minus the costs and expenses of the year",
     "url": LAW + "G0340003&flno=24"},
    {"label": "Profit-seeking Enterprise Income Tax Audit Regulations Art. 67: an expense without its own original "
              "voucher is not recognised",
     "url": LAW + "G0340051&flno=67"},
]
CITE_PERIOD_EARLY = [
    {"label": "VAT Enforcement Rules Art. 29: input tax not claimed in its period may be claimed in the next; "
              "after that, with a stated reason, for up to ten years",
     "url": LAW + "G0340081&flno=29"},
]

# The six issue types that are evaluated. READ_REVIEW is a routing note, not a finding.
ISSUE_TYPES = list(RULES.keys())
RULES["READ_REVIEW"] = {
    "title": "Needs a human look: a field could not be confirmed on the page",
    "severity": "review",
    "citations": [{"label": "Not a finding. Ledger Room only reports a checksum failure when the digits are confirmed "
                            "by OCR or by two of three independent reads.", "url": None}],
}
# Routing rules: they decide whether a line may go into the filing, but they are not among the six evaluated issue
# types (the answer keys were frozen before they existed). Their counts are reported separately.
RULES["AMOUNT_MISMATCH"] = {
    "title": "Hold: the amounts do not add up (net + tax = total, tax = 5% of net)",
    "severity": "hold",
    "citations": [{"label": "Arithmetic check, not a legal finding: on a voucher with tax, net + tax must equal the total "
                            "(NT$1 rounding allowed). Usually a misread amount; fix it before the line is exported.",
                   "url": None}],
}
RULES["BUYER_NOT_CLIENT"] = {
    "title": "No deductible input tax: the buyer UBN is missing or is not this client",
    "severity": "medium",
    "citations": [
        {"label": "VAT Act Art. 19(1)(1): input tax is not deductible without the vouchers required by Art. 33",
         "url": LAW + "G0340080&flno=19"},
        {"label": "VAT Act Art. 33: deductible vouchers must carry the buyer's name, address and UBN",
         "url": LAW + "G0340080&flno=33"},
    ],
}
RULES["BUYER_PRESUMED"] = {
    "title": "Hold: buyer UBN could not be read; the buyer name looks like this client",
    "severity": "hold",
    "citations": [{"label": "Not deducted on a guess: VAT Act Art. 33 requires the buyer UBN on the voucher. "
                            "Check the paper before deducting.", "url": LAW + "G0340080&flno=33"}],
}
RULES["NOT_A_VOUCHER"] = {
    "title": "Not a tax voucher on its own: transaction detail or payment slip without an invoice number",
    "severity": "hold",
    "citations": [{"label": "Profit-seeking Enterprise Income Tax Audit Regulations Art. 67: an expense needs its original "
                            "voucher; a payment slip or an order detail without an invoice number is not one. Ask the client "
                            "for the invoice (or find it on their e-invoice carrier).", "url": LAW + "G0340051&flno=67"}],
}
RULES["SMALL_RECEIPT_CAP"] = {
    "title": "Plain receipts from small businesses above 3% of the year's expenses",
    "severity": "warning",
    "citations": [{"label": "Profit-seeking Enterprise Income Tax Audit Regulations Art. 67(3): plain receipts (普通收據) from "
                            "small businesses count only up to 3% (千分之三十) of the year's manufacturing and operating "
                            "expenses", "url": LAW + "G0340051&flno=67"}],
}

# Optional, only when the firm keeps a local copy of the MOF business-registration open data (see registry.py).
# Not one of the six evaluated issue types: the synthetic UBNs are random and are not in any register.
RULES["UBN_NOT_REGISTERED"] = {
    "title": "UBN passes the checksum but is not in the business-registration file",
    "severity": "warning",
    "citations": [{"label": "MOF Fiscal Information Agency open data: national business (tax) registration file "
                            "(local copy; see registry.py for the date of the copy)",
                   "url": "https://data.gov.tw/dataset/9400"}],
}

# --- category lexicon (auditable keyword table; the model's category is only a fallback) ---
S19_LEXICON: list[tuple[str, str, list[str]]] = [
    # (category, Art.19(1) item, keywords)
    ("entertainment", "item 3 (entertainment 交際應酬)",
     ["禮盒", "礼盒", "伴手禮", "伴手礼", "宴客", "宴請", "宴请", "餐敘", "招待", "紅酒", "红酒", "威士忌", "高粱",
      "花籃", "花篮", "賀禮", "贺礼", "喜宴", "交際", "交际",   # simplified variants: OCR output
      # added after the real e-invoice check (2026-10-14) found beer and cocktails that the table missed
      "啤酒", "調酒", "调酒", "清酒", "葡萄酒", "香檳", "梅酒", "燒酎", "雞尾酒"]),
    ("employee_benefit", "item 4 (employee personal benefits 酬勞員工)",
     ["員工旅遊", "員工旅游", "员工旅游", "尾牙", "員工禮品", "員工礼品", "員工獎品", "生日禮", "三節禮", "員工聚餐"]),
    ("passenger_car", "item 5 (passenger car for own use 自用乘人小汽車)",
     ["小客車", "小客车", "轎車", "轿车", "自用車", "自用车"]),
    ("non_business", "item 2 (not for business use 非供本業使用)",
     ["家用", "私人", "個人用"]),
]
S19_MODEL_CATEGORIES = {c for c, _, _ in S19_LEXICON}
# Alcohol and meals are often entertainment, but a restaurant buys beer to resell and staff travel meals can be
# business costs. A match on these words is reported as "to confirm", not as a finding.
TO_CONFIRM_KW = {"紅酒", "红酒", "威士忌", "高粱", "啤酒", "調酒", "调酒", "清酒", "葡萄酒", "香檳", "梅酒", "燒酎", "雞尾酒",
                 "宴客", "宴請", "宴请", "餐敘", "喜宴", "員工聚餐"}

EXPENSE_LEXICON: list[tuple[str, list[str]]] = [
    ("taxi", ["計程車", "車資", "乘車", "叫車"]),
    ("lodging", ["住宿", "旅館", "飯店", "旅店", "民宿", "商旅"]),
    ("meal", ["餐", "便當", "咖啡", "飲料", "火鍋", "麵", "飯", "壽司", "披薩", "燒肉", "小吃", "合菜"]),
]


@dataclass
class Item:
    desc: str
    qty: float | None = None
    amount: int | None = None


@dataclass
class Doc:
    doc_id: str
    client_id: str
    month: str                      # folder month, e.g. "2026-09"
    doc_type: str | None = None     # e_invoice | triplicate_invoice | receipt
    invoice_no: str | None = None   # normalised "AB12345678"
    date: date | None = None
    invoice_period: str | None = None   # e-invoice header period normalised "2026-09/2026-10"
    seller_name: str | None = None
    seller_ubn: str | None = None
    buyer_name: str | None = None
    buyer_ubn: str | None = None
    items: list[Item] = field(default_factory=list)
    net: int | None = None
    tax: int | None = None
    total: int | None = None
    note: str | None = None
    model_category: str | None = None   # category proposed by the model (fallback only)
    doc_kind: str | None = None          # transaction_detail | payment_slip | None, from printed keywords (voucher_kind)
    uncertain: dict = field(default_factory=dict)   # field -> why it is not confirmed on the page


@dataclass
class Exception_:
    doc_id: str
    client_id: str
    rule_id: str
    severity: str
    message: str
    evidence_fields: list[str]
    related_docs: list[str] = field(default_factory=list)
    confidence: str = "high"        # high = keyword/arith evidence; medium = model-suggested category
    citations: list[dict] | None = None   # overrides the rule's default citations for this exception

    def to_dict(self) -> dict:
        r = RULES[self.rule_id]
        return {
            "doc_id": self.doc_id,
            "client_id": self.client_id,
            "rule_id": self.rule_id,
            "title": r["title"],
            "severity": self.severity,
            "message": self.message,
            "evidence_fields": self.evidence_fields,
            "related_docs": self.related_docs,
            "confidence": self.confidence,
            "citations": self.citations if self.citations is not None else r["citations"],
        }


# ---------------- helpers ----------------

def norm_invoice_no(s: str | None) -> str | None:
    if not s:
        return None
    t = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    return t if re.fullmatch(r"[A-Z]{2}\d{8}", t) else None


# OCR often returns simplified characters (交易明细, 交易类别), so both forms are listed.
PAYMENT_SLIP_KW = ("多元支付", "支付服務", "支付服务", "交易類別", "交易类别", "交易類别", "付款類別", "付款类别", "簽帳單",
                   "签帐单", "簽單", "端末代號", "端末代号")
DETAIL_KW = ("交易明細", "交易明细", "消費明細", "消费明细", "點餐明細", "点餐明细", "結帳單", "结帐单")
INVOICE_PROOF_KW = ("證明聯", "证明联", "發票證明", "发票证明", "發票证明", "统一發票", "統一發票", "统一发票")


def voucher_kind(ocr_text: str, invoice_no: str | None) -> str | None:
    """Printed hints that a slip is not an invoice: a payment terminal slip, or an order / transaction detail.
    A slip that prints the words of an invoice proof (證明聯, 統一發票) is never reclassified. A transaction detail
    can print the number of the e-invoice that went to a carrier; it is still not the voucher, so a read invoice
    number does not clear it."""
    if any(k in ocr_text for k in INVOICE_PROOF_KW):
        return None
    if any(k in ocr_text for k in PAYMENT_SLIP_KW):
        return "payment_slip"
    if any(k in ocr_text for k in DETAIL_KW):
        return "transaction_detail"
    return None


def doc_text(d: Doc) -> str:
    return " ".join([i.desc for i in d.items if i.desc] + [d.note or ""])


def s19_category(d: Doc) -> tuple[str | None, str | None, str]:
    """Return (category, matched keyword, confidence)."""
    text = doc_text(d)
    for cat, _art, kws in S19_LEXICON:
        for kw in kws:
            if kw in text:
                return cat, kw, "high"
    if d.model_category in S19_MODEL_CATEGORIES:
        return d.model_category, None, "medium"
    return None, None, ""


# Seller names only count with multi-character terms: a single misread character in a shop name
# (e.g. OCR reading 保養廠 as 保餐廠) must not turn a garage into a restaurant.
SELLER_LEXICON: list[tuple[str, list[str]]] = [
    ("taxi", ["計程車", "車隊", "衛星"]),
    ("lodging", ["商旅", "飯店", "旅館", "民宿", "酒店"]),
    ("meal", ["餐廳", "餐館", "餐飲", "麵館", "牛肉麵", "咖啡", "小吃", "火鍋", "食堂", "便當"]),
]


def expense_category(d: Doc, client_name: str | None = None) -> tuple[str | None, str | None]:
    text = doc_text(d)
    for cat, kws in EXPENSE_LEXICON:
        for kw in kws:
            if kw in text:
                return cat, kw
    # the seller name is a legitimate signal here (e.g. "...餐廳"), unlike for Art. 19 -
    # but never when it is really the client's own name copied into the seller field
    seller = d.seller_name or ""
    if not (client_name and name_sim(seller, client_name) >= 0.8):
        for cat, kws in SELLER_LEXICON:
            for kw in kws:
                if kw in seller:
                    return cat, kw
    if d.model_category in {c for c, _ in EXPENSE_LEXICON}:
        return d.model_category, None
    return None, None


TAX_VOUCHERS = ("e_invoice", "triplicate_invoice", "two_part_register")


def buyer_presumed(d: Doc, client: dict) -> bool:
    """Buyer UBN unreadable on a triplicate invoice, but the printed buyer name looks like this client."""
    return (d.buyer_ubn is None and d.doc_type == "triplicate_invoice"
            and name_sim(d.buyer_name, client.get("name")) >= 0.7)


def amounts_inconsistent(net, tax, total) -> bool:
    """net + tax != total, or tax is not 5% of net, beyond NT$1 rounding (only when all three are known and tax > 0).
    Assumes the standard 5% rate; zero-rated or special-rate vouchers would need their own rate."""
    if net is None or tax is None or total is None or not tax:
        return False
    return abs(net + tax - total) > 1 or abs(tax - net * 0.05) > 1


def claimable_input_tax(d: Doc, client: dict, allow_presumed: bool = False) -> int:
    """Input tax this client could deduct from this voucher (0 if none). allow_presumed=True also counts a voucher
    whose buyer UBN could not be read but whose buyer name matches: used to explain risk, never to deduct."""
    if d.doc_type not in TAX_VOUCHERS:
        return 0
    if d.buyer_ubn != client["ubn"] and not (allow_presumed and buyer_presumed(d, client)):
        return 0
    if d.tax:
        return d.tax
    if d.total:
        return d.total - round(d.total / 1.05)
    return 0


def _hamming(a: str, b: str) -> int:
    if len(a) != len(b):
        return 99
    return sum(x != y for x, y in zip(a, b))


# ---------------- the engine ----------------

def run_rules(docs: list[Doc], clients: dict[str, dict], registry=None) -> list[Exception_]:
    """registry: optional ledger_room.registry.Registry (local copy of the MOF business-registration file).
    With it, a checksum failure that is one digit away from a registered business is reported as a likely misread,
    and a well-formed number missing from the register gets a UBN_NOT_REGISTERED warning."""
    out: list[Exception_] = []

    for d in docs:
        c = clients[d.client_id]
        # 1. UBN checksum (+ optional registration lookup)
        for role in ("seller", "buyer"):
            if d.doc_kind == "payment_slip":
                break   # the 8 digits on a payment slip are a terminal or store id, not a UBN; the slip is held anyway
            v = getattr(d, f"{role}_ubn")
            why = d.uncertain.get(f"{role}_ubn")
            if why:
                out.append(Exception_(d.doc_id, d.client_id, "READ_REVIEW", "review",
                                      f"{role.capitalize()} UBN not confirmed on the page ({why}); check it by eye.",
                                      [f"{role}_ubn"], confidence="low"))
                continue
            if not v:
                continue
            chk = ubnlib.check(v)
            if chk.well_formed and not chk.valid_new:
                msg = f"{role.capitalize()} UBN {v}: {chk.explanation}."
                if registry is not None:
                    near = registry.near_misses(v)
                    msg += (" One digit away from registered "
                            + "; ".join(f"{u} {n}" for u, n in near[:3])
                            + ": more likely a misread or a typo than a fake number; compare with the paper."
                            if near else " No registered business is one digit away.")
                out.append(Exception_(d.doc_id, d.client_id, "UBN_CHECKSUM", "high", msg, [f"{role}_ubn"]))
            elif chk.well_formed and registry is not None and v != c.get("ubn"):
                name = registry.name(v)
                if name is None:
                    out.append(Exception_(d.doc_id, d.client_id, "UBN_NOT_REGISTERED", "warning",
                                          f"{role.capitalize()} UBN {v} passes the checksum but is not in the "
                                          f"registration file ({registry.label}). New or closed business, or a made-up "
                                          "number: check before deducting.", [f"{role}_ubn"]))

        # 4. Art.19 (a held voucher with a presumed buyer is still checked, so the risk is visible)
        tax = claimable_input_tax(d, c, allow_presumed=True)
        if tax > 0:
            cat, kw, conf = s19_category(d)
            if cat:
                art = next(a for cc, a, _ in S19_LEXICON if cc == cat)
                why = f"keyword '{kw}'" if kw else "model-suggested category (no keyword match; confirm)"
                if kw in TO_CONFIRM_KW:
                    out.append(Exception_(d.doc_id, d.client_id, "S19_NONDEDUCTIBLE", "warning",
                                          f"To confirm: input tax NT${tax:,} is not deductible if this was entertainment "
                                          f"or a staff benefit (Art. 19(1) {art}); {why}. Deductible if bought for resale "
                                          "or as a business cost.", ["items", "note"], confidence=conf))
                else:
                    out.append(Exception_(d.doc_id, d.client_id, "S19_NONDEDUCTIBLE", "medium",
                                          f"Input tax NT${tax:,} is not deductible: Art. 19(1) {art}; {why}.",
                                          ["items", "note"], confidence=conf))

        # 7. deductibility of the voucher itself (Art. 19(1)(1), Art. 33)
        if d.doc_type in TAX_VOUCHERS:
            if buyer_presumed(d, c):
                out.append(Exception_(d.doc_id, d.client_id, "BUYER_PRESUMED", "hold",
                                      f"Buyer UBN not readable; printed buyer name '{d.buyer_name}' looks like this client. "
                                      "Held: not deducted until the UBN is checked on the paper.", ["buyer_ubn"]))
            elif d.buyer_ubn and d.buyer_ubn != c["ubn"]:
                out.append(Exception_(d.doc_id, d.client_id, "BUYER_NOT_CLIENT", "medium",
                                      f"Buyer UBN {d.buyer_ubn} is not this client's ({c['ubn']}): its input tax cannot be "
                                      "deducted by this client.", ["buyer_ubn"]))
            elif not d.buyer_ubn:
                out.append(Exception_(d.doc_id, d.client_id, "BUYER_NOT_CLIENT", "warning",
                                      "No buyer UBN on the voucher (consumer-style invoice): an expense voucher only, "
                                      "no deductible input tax.", ["buyer_ubn"]))

        # 7b. slips that are not vouchers
        if d.doc_kind or (d.doc_type in (None, "receipt") and not d.invoice_no and not d.seller_ubn):
            kind = {"payment_slip": "a payment slip", "transaction_detail": "a transaction detail"}.get(d.doc_kind,
                                                                                                    "a slip without seller UBN or invoice number")
            out.append(Exception_(d.doc_id, d.client_id, "NOT_A_VOUCHER", "hold",
                                  f"This looks like {kind}, not an invoice or receipt with the seller's UBN. Held: ask the "
                                  "client for the invoice before it is booked.", ["invoice_no", "seller_ubn"]))

        # 8. arithmetic: net + tax = total and tax = 5% of net; a deductible line also needs both amounts read
        if amounts_inconsistent(d.net, d.tax, d.total):
            out.append(Exception_(d.doc_id, d.client_id, "AMOUNT_MISMATCH", "hold",
                                  f"Net NT${d.net:,} + tax NT${d.tax:,} (total NT${d.total if d.total is not None else 0:,}) "
                                  "do not add up, or the tax is not 5% of the net. Probably a misread amount; held until "
                                  "corrected.", ["net", "tax", "total"]))
        elif (d.doc_type in TAX_VOUCHERS and d.buyer_ubn == c["ubn"]
              and (d.net is None or d.tax is None)):
            out.append(Exception_(d.doc_id, d.client_id, "AMOUNT_MISMATCH", "hold",
                                  "Net or tax not read on a voucher this client would deduct; held until both are filled in.",
                                  ["net", "tax", "total"]))

        # 5. policy limit
        limits = c.get("policy", {})
        cat, kw = expense_category(d, c.get("name"))
        if d.total is not None:
            if cat and cat in limits and d.total > limits[cat]:
                out.append(Exception_(d.doc_id, d.client_id, "OVER_LIMIT", "medium",
                                      f"{cat} total NT${d.total:,} exceeds the client limit NT${limits[cat]:,} per document"
                                      + (f" (matched '{kw}')." if kw else "."), ["total", "items"]))
            elif "any" in limits and d.total > limits["any"]:
                out.append(Exception_(d.doc_id, d.client_id, "OVER_LIMIT", "medium",
                                      f"Total NT${d.total:,} exceeds the client's approval threshold NT${limits['any']:,}.",
                                      ["total"]))

        # 6. period
        p0, p1 = date.fromisoformat(c["period"][0]), date.fromisoformat(c["period"][1])
        if d.date:
            if d.date < p0:
                # a warning, not an error: Art. 29 lets input tax be claimed in a later period
                out.append(Exception_(d.doc_id, d.client_id, "PERIOD_MISMATCH", "warning",
                                      f"Dated {d.date.isoformat()}, before the filing period {p0}..{p1}. Allowed as a "
                                      "deferred claim (Enforcement Rules Art. 29; from the second period on, with a "
                                      "stated reason). Confirm it was not already claimed in its own period.",
                                      ["date"], citations=CITE_PERIOD_EARLY))
            elif d.date > p1:
                out.append(Exception_(d.doc_id, d.client_id, "PERIOD_MISMATCH", "medium",
                                      f"Dated {d.date.isoformat()}, after the filing period ends ({p1}).",
                                      ["date"]))
            elif d.invoice_period:
                a, b = d.invoice_period.split("/")
                if d.date.strftime("%Y-%m") not in (a, b):
                    out.append(Exception_(d.doc_id, d.client_id, "PERIOD_MISMATCH", "medium",
                                          f"Printed invoice period {d.invoice_period} does not contain the printed date "
                                          f"{d.date.isoformat()} (internal inconsistency).", ["date", "invoice_period"]))

    # 9. Audit Regulations Art. 67(3): plain receipts from small businesses, cumulative per client (only when the
    #    client's yearly manufacturing + operating expenses are configured as "annual_expenses")
    run_sum: dict[str, int] = defaultdict(int)
    for d in sorted(docs, key=lambda x: (x.client_id, x.date or date.min, x.doc_id)):
        c = clients[d.client_id]
        cap_base = c.get("annual_expenses")
        if not cap_base or d.doc_type != "receipt" or d.tax or d.total is None:
            continue
        run_sum[d.client_id] += d.total
        cap = round(cap_base * 0.03)
        if run_sum[d.client_id] > cap:
            out.append(Exception_(d.doc_id, d.client_id, "SMALL_RECEIPT_CAP", "warning",
                                  f"Plain receipts so far NT${run_sum[d.client_id]:,} exceed 3% of the client's yearly "
                                  f"expenses (NT${cap:,}); the excess is not recognised as an expense. Assumes the "
                                  "seller is a small business issuing plain receipts.", ["total"]))

    # 2/3. duplicates: union-find over invoice number, or seller+date+total
    parent = list(range(len(docs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    by_no: dict[str, list[int]] = defaultdict(list)
    by_comp: dict[tuple, list[int]] = defaultdict(list)
    for i, d in enumerate(docs):
        if d.invoice_no:
            by_no[d.invoice_no].append(i)
        if d.seller_ubn and d.date and d.total is not None:
            by_comp[(d.seller_ubn, d.date, d.total)].append(i)
        elif not d.seller_ubn and not d.invoice_no and d.date and d.total and d.total >= 100:
            # slips with neither seller UBN nor invoice number (a detail and its payment slip): date + amount only,
            # always reported as a possible duplicate
            by_comp[("no-ubn", d.date, d.total)].append(i)
    # a payment slip or a transaction detail is paired on date + amount with any other document of that day
    by_day: dict[tuple, list[int]] = defaultdict(list)
    for i, d in enumerate(docs):
        if d.date and d.total and d.total >= 100:
            by_day[(d.date, d.total)].append(i)
    for idx in by_day.values():
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                if docs[idx[a]].doc_kind or docs[idx[b]].doc_kind:
                    union(idx[a], idx[b])
    for idx in by_no.values():
        for j in idx[1:]:
            union(idx[0], j)
    for idx in by_comp.values():
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                da, db = docs[idx[a]], docs[idx[b]]
                if not da.invoice_no or not db.invoice_no or _hamming(da.invoice_no, db.invoice_no) <= 1:
                    union(idx[a], idx[b])
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(docs)):
        groups[find(i)].append(i)
    for members in groups.values():
        if len(members) < 2:
            continue
        ms = sorted(members, key=lambda i: (docs[i].month, docs[i].client_id, docs[i].doc_id))
        for pos, i in enumerate(ms):
            d = docs[i]
            earlier_same = [docs[j] for j in ms[:pos] if docs[j].client_id == d.client_id]
            others = [docs[j] for j in ms if docs[j].client_id != d.client_id]
            tax = claimable_input_tax(d, clients[d.client_id], allow_presumed=True)
            same_no = lambda o: bool(d.invoice_no) and d.invoice_no == o.invoice_no   # noqa: E731
            why = (" This client would deduct its input tax twice." if tax > 0 else
                   " It carries no input tax for this client, so the risk is the same expense counted twice "
                   "(income tax), not over-claimed VAT.")
            slip = d.doc_kind or any(docs[j].doc_kind for j in ms if j != i)
            maybe = ("Possible duplicate (same date and amount; one is a payment slip or transaction detail, which "
                     "often travels with the invoice or detail for the same purchase): " if slip else
                     "Possible duplicate (same seller, date and amount, no shared invoice number; two same-day "
                     "purchases at the same price are normal, so check with the client): ")
            if earlier_same:
                e = earlier_same[0]
                sure = same_no(e)
                out.append(Exception_(d.doc_id, d.client_id, "DUP_CROSS_MONTH", "high" if sure else "warning",
                                      ("" if sure else maybe) +
                                      f"Same voucher already submitted by this client ({e.month}: {e.doc_id})." + why,
                                      ["invoice_no", "seller_ubn", "date", "total"], related_docs=[e.doc_id],
                                      citations=CITE_DUP_VAT if tax > 0 else CITE_DUP_INCOME))
            if others:
                sure = any(same_no(o) for o in others)
                out.append(Exception_(d.doc_id, d.client_id, "DUP_CROSS_CLIENT", "high" if sure else "warning",
                                      ("" if sure else maybe) + "Same voucher also appears under client(s) "
                                      + ", ".join(sorted({o.client_id for o in others})) + "." + why,
                                      ["invoice_no", "seller_ubn", "date", "total"],
                                      related_docs=[o.doc_id for o in others],
                                      citations=CITE_DUP_VAT_CROSS if tax > 0 else CITE_DUP_INCOME))
    return out


def issues_by_doc(exceptions: list[Exception_]) -> dict[str, set[str]]:
    m: dict[str, set[str]] = defaultdict(set)
    for e in exceptions:
        if e.rule_id in ISSUE_TYPES:
            m[e.doc_id].add(e.rule_id)
    return m
