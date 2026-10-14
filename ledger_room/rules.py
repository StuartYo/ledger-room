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
            {"label": "VAT Enforcement Rules Art. 29: a voucher not filed in its period may be filed in the next; "
                      "later than that requires a written reason",
             "url": LAW + "G0340081&flno=29"},
        ],
    },
}

# The six issue types that are evaluated. READ_REVIEW is a routing note, not a finding.
ISSUE_TYPES = list(RULES.keys())
RULES["READ_REVIEW"] = {
    "title": "Needs a human look: a field could not be confirmed on the page",
    "severity": "review",
    "citations": [{"label": "Not a finding. Ledger Room only reports a checksum failure when the digits are confirmed "
                            "by OCR or by two of three independent reads.", "url": None}],
}

# --- category lexicon (auditable keyword table; the model's category is only a fallback) ---
S19_LEXICON: list[tuple[str, str, list[str]]] = [
    # (category, Art.19(1) item, keywords)
    ("entertainment", "item 3 (entertainment 交際應酬)",
     ["禮盒", "礼盒", "伴手禮", "伴手礼", "宴客", "宴請", "宴请", "餐敘", "招待", "紅酒", "红酒", "威士忌", "高粱",
      "花籃", "花篮", "賀禮", "贺礼", "喜宴", "交際", "交际"]),   # simplified variants: OCR output
    ("employee_benefit", "item 4 (employee personal benefits 酬勞員工)",
     ["員工旅遊", "員工旅游", "员工旅游", "尾牙", "員工禮品", "員工礼品", "員工獎品", "生日禮", "三節禮", "員工聚餐"]),
    ("passenger_car", "item 5 (passenger car for own use 自用乘人小汽車)",
     ["小客車", "小客车", "轎車", "轿车", "自用車", "自用车"]),
    ("non_business", "item 2 (not for business use 非供本業使用)",
     ["家用", "私人", "個人用"]),
]
S19_MODEL_CATEGORIES = {c for c, _, _ in S19_LEXICON}

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
            "citations": r["citations"],
        }


# ---------------- helpers ----------------

def norm_invoice_no(s: str | None) -> str | None:
    if not s:
        return None
    t = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    return t if re.fullmatch(r"[A-Z]{2}\d{8}", t) else None


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


def claimable_input_tax(d: Doc, client: dict) -> int:
    """Input tax this client would deduct from this voucher (0 if none)."""
    if d.doc_type not in ("e_invoice", "triplicate_invoice"):
        return 0
    presumed = (d.buyer_ubn is None and d.doc_type == "triplicate_invoice"
                and name_sim(d.buyer_name, client.get("name")) >= 0.7)   # buyer UBN unreadable, name matches
    if d.buyer_ubn != client["ubn"] and not presumed:
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

def run_rules(docs: list[Doc], clients: dict[str, dict]) -> list[Exception_]:
    out: list[Exception_] = []

    for d in docs:
        c = clients[d.client_id]
        # 1. UBN checksum
        for role in ("seller", "buyer"):
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
                out.append(Exception_(d.doc_id, d.client_id, "UBN_CHECKSUM", "high",
                                      f"{role.capitalize()} UBN {v}: {chk.explanation}.",
                                      [f"{role}_ubn"]))

        # 4. Art.19
        tax = claimable_input_tax(d, c)
        if tax > 0:
            cat, kw, conf = s19_category(d)
            if cat:
                art = next(a for cc, a, _ in S19_LEXICON if cc == cat)
                why = f"keyword '{kw}'" if kw else "model-suggested category (no keyword match; confirm)"
                out.append(Exception_(d.doc_id, d.client_id, "S19_NONDEDUCTIBLE", "medium",
                                      f"Input tax NT${tax:,} is not deductible: Art. 19(1) {art}; {why}.",
                                      ["items", "note"], confidence=conf))

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
                out.append(Exception_(d.doc_id, d.client_id, "PERIOD_MISMATCH", "medium",
                                      f"Dated {d.date.isoformat()}, before the filing period {p0}..{p1}. "
                                      "Prior-period voucher: needs a written reason if filed late (Enforcement Rules Art. 29).",
                                      ["date"]))
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
            if earlier_same:
                e = earlier_same[0]
                out.append(Exception_(d.doc_id, d.client_id, "DUP_CROSS_MONTH", "high",
                                      f"Same voucher already submitted by this client ({e.month}: {e.doc_id}).",
                                      ["invoice_no", "seller_ubn", "date", "total"], related_docs=[e.doc_id]))
            if others:
                out.append(Exception_(d.doc_id, d.client_id, "DUP_CROSS_CLIENT", "high",
                                      "Same voucher also appears under client(s) "
                                      + ", ".join(sorted({o.client_id for o in others})) + ".",
                                      ["invoice_no", "seller_ubn", "date", "total"],
                                      related_docs=[o.doc_id for o in others]))
    return out


def issues_by_doc(exceptions: list[Exception_]) -> dict[str, set[str]]:
    m: dict[str, set[str]] = defaultdict(set)
    for e in exceptions:
        if e.rule_id in ISSUE_TYPES:
            m[e.doc_id].add(e.rule_id)
    return m
