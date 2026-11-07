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
            {"label": "VAT Act Art. 51(1)(5): claiming the same input tax twice may constitute over-claimed input tax (虛報進項稅額), fined up to 5x the tax evaded",
             "url": LAW + "G0340080&flno=51"},
        ],
    },
    "DUP_CROSS_CLIENT": {
        "title": "Duplicate voucher: the same document appears under two different clients",
        "severity": "high",
        # Mostly expense receipts without a buyer UBN (a voucher with the buyer's UBN is already stopped by
        # BUYER_NOT_CLIENT for the wrong client), so the expense rule leads; the VAT rules apply only when input tax
        # is claimed. Each exception carries the citations that fit its voucher (CITE_DUP_INCOME / CITE_DUP_VAT_CROSS).
        "citations": [
            {"label": "Profit-seeking Enterprise Income Tax Audit Regulations Art. 67(1): an expense without an original "
                      "voucher, or whose voucher does not match, is in principle not recognised",
             "url": LAW + "G0340051&flno=67"},
            {"label": "VAT Act Art. 33 (only when input tax is claimed): deductible vouchers must carry the buyer's name, "
                      "address and UBN", "url": LAW + "G0340080&flno=33"},
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
    {"label": "VAT Act Art. 51(1)(5): claiming the same input tax twice may constitute over-claimed input tax (虛報進項稅額), fined up to 5x the tax evaded", "url": LAW + "G0340080&flno=51"},
]
CITE_ART67 = {"label": "Profit-seeking Enterprise Income Tax Audit Regulations Art. 67(1): an expense without an original "
                       "voucher, or whose voucher does not match, is in principle not recognised",
              "url": LAW + "G0340051&flno=67"}
# Across clients the expense rule leads (G15); VAT Art. 33 and Art. 51 follow only because input tax is claimed here.
CITE_DUP_VAT_CROSS = [CITE_ART67,
                      {"label": "VAT Act Art. 33 (input tax is claimed on this voucher): deductible vouchers must carry "
                                "the buyer's name, address and UBN", "url": LAW + "G0340080&flno=33"}] + CITE_DUP_VAT
CITE_DUP_INCOME = [
    CITE_ART67,
    {"label": "Income Tax Act Art. 24: taxable income is revenue minus the costs and expenses of the year",
     "url": LAW + "G0340003&flno=24"},
]
ART38_URL = LAW + "G0340081&flno=38"
CITE_ART38_OTHER = [
    {"label": "VAT Enforcement Rules Art. 38(1)(13): only other vouchers showing VAT that the Ministry of Finance has "
              "approved (財政部核定之載有稅額之其他憑證) count as input-tax vouchers", "url": ART38_URL},
    {"label": "VAT Act Art. 19(1)(1): input tax is not deductible without the vouchers required by Art. 33",
     "url": LAW + "G0340080&flno=19"},
]
CITE_ART38_TRANSPORT = [
    {"label": "VAT Enforcement Rules Art. 38(1)(10) and (4): receipts or ticket stubs of trains / buses (火（汽）車), "
              "high-speed rail, ships and aircraft issued to employees on business trips; input tax = total x 5% / "
              "(1 + 5%), rounded to the dollar", "url": ART38_URL},
]
CITE_ART38_CUSTOMS = [
    {"label": "VAT Enforcement Rules Art. 38(1)(2): the deduction copy of the customs-collected VAT payment "
              "certificate (海關代徵營業稅繳納證扣抵聯)", "url": ART38_URL},
    {"label": "營業稅電子資料申報繳稅作業要點 第二十點 and 附件五: format code 28; certificate number 14 characters "
              "(positions 36-49); amount field = 營業稅稅基 on the certificate",
     "url": "https://law-out.mof.gov.tw/LawContent.aspx?id=GL009478"},
]
CITE_TWO_PART = [
    {"label": "VAT Act Art. 33: deductible vouchers must carry the buyer's name, address and UBN",
     "url": LAW + "G0340080&flno=33"},
    {"label": "VAT Enforcement Rules Art. 38(1): a two-part cash-register invoice counts only when it carries the "
              "buyer's UBN", "url": LAW + "G0340081&flno=38"},
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
    "citations": [{"label": "Profit-seeking Enterprise Income Tax Audit Regulations Art. 67(1): an expense without an "
                            "original voucher is in principle not recognised. A payment slip or an order detail without an "
                            "invoice number is supporting paper, not the voucher: ask the client for the invoice (or find it "
                            "on their e-invoice carrier).", "url": LAW + "G0340051&flno=67"}],
}
RULES["SMALL_RECEIPT_CAP"] = {
    "title": "Plain receipts from small businesses above 3% of manufacturing + operating expenses",
    "severity": "warning",
    "citations": [{"label": "Profit-seeking Enterprise Income Tax Audit Regulations Art. 67(3): plain receipts (普通收據) issued "
                            "by small businesses count, per year, only up to 3% (千分之三十) of the year's manufacturing "
                            "expenses plus operating expenses as assessed by the tax office; the excess is not recognised. "
                            "Applies to small-business plain receipts only.", "url": LAW + "G0340051&flno=67"}],
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

# Voucher kinds and 401 details added after the bookkeeping-practice review (gap rows G4, G5, G6, G8, G1 hook).
# None of them is among the six evaluated issue types.
RULES["RECEIPT_TAX_UNVERIFIED"] = {
    "title": "Hold: receipt shows a tax amount; only MOF-approved voucher kinds carry deductible input tax",
    "severity": "hold",
    "citations": CITE_ART38_OTHER,
}
RULES["TRANSPORT_TICKET"] = {
    "title": "Transport ticket: input tax 5/105 of the total (format code 22); confirm it was an employee's business trip",
    "severity": "warning",
    "citations": CITE_ART38_TRANSPORT,
}
RULES["CUSTOMS_KEY_BY_HAND"] = {
    "title": "Hold: customs-collected VAT payment certificate (format code 28): key the certificate number and tax by hand",
    "severity": "hold",
    "citations": CITE_ART38_CUSTOMS,
}
RULES["FIXED_ASSET_HINT"] = {
    "title": "May be a fixed asset: confirm deduction code 2 (or 4 if non-deductible)",
    "severity": "warning",
    "citations": [{"label": "Profit-seeking Enterprise Income Tax Audit Regulations Art. 77-1: an item with a useful life "
                            "over two years and an amount over NT$80,000 is a capital expenditure; the 401 return splits "
                            "input lines into purchases and expenses (deduction code 1 / 3) and fixed assets (2 / 4)",
                   "url": LAW + "G0340051&flno=77-1"}],
}
RULES["VOID_ON_PLATFORM"] = {
    "title": "Voided on the e-invoice platform: do not claim",
    "severity": "high",
    "citations": [{"label": "The client's e-invoice platform download marks this invoice number 作廢 (課稅別 F). A voided "
                            "invoice carries no input tax; claiming it is over-claiming (VAT Act Art. 51(1)(5)).",
                   "url": LAW + "G0340080&flno=51"}],
}
RULES["UTILITY_BILL_PLATFORM"] = {
    "title": "Utility bill / payment notice: book the input tax from the platform e-invoice, not from this paper",
    "severity": "info",
    "citations": [
        {"label": "VAT Act Art. 33: the input-tax voucher is the uniform invoice. A utility (electricity, water, telecom, "
                  "gas) issues an e-invoice for the bill, and the client's copy is in the MOF e-invoice platform "
                  "download; the paper bill is a payment notice / proof of payment, kept with the books but not keyed",
         "url": LAW + "G0340080&flno=33"},
        {"label": "VAT Act Art. 51(1)(5): keying the bill as well as importing its e-invoice would claim the same input "
                  "tax twice", "url": LAW + "G0340080&flno=51"},
    ],
}
RULES["IN_PLATFORM"] = {
    "title": "Already in the e-invoice platform download: not entered again",
    "severity": "info",
    "citations": [{"label": "Not a finding. The invoice number is in the client's e-invoice platform download for this "
                            "period, which is imported on its own; entering the photo too would claim the same input tax "
                            "twice (VAT Act Art. 51(1)(5)).", "url": LAW + "G0340080&flno=51"}],
}
# Period check (period_check.py): rules that need the whole period at once, the platform download, the client's
# earlier filed media files and the paper together. Not among the six evaluated issue types.
MEDIA_RULE_URL = "https://law-out.mof.gov.tw/LawContent.aspx?id=GL009478"
RULES["DUP_PRIOR_FILING"] = {
    "title": "Already claimed in an earlier period's filing",
    "severity": "high",
    "citations": [
        {"label": "VAT Act Art. 51(1)(5): claiming the same input tax twice may constitute over-claimed input tax "
                  "(虛報進項稅額), fined up to 5x the tax evaded. The voucher number (and seller UBN) is in a media file "
                  "this client already filed for an earlier period", "url": LAW + "G0340080&flno=51"},
        {"label": "VAT Enforcement Rules Art. 29: a voucher not claimed in its own period may be claimed later; one that "
                  "was already claimed may not be claimed again", "url": LAW + "G0340081&flno=29"},
    ],
}
RULES["PLATFORM_AMOUNT_DIFF"] = {
    "title": "Hold: the photographed invoice and the platform record disagree",
    "severity": "hold",
    "citations": [
        {"label": "營業稅電子資料申報繳稅作業要點 第二十點 and 附件五: the input record carries the voucher's own sales "
                  "amount and tax. The platform line and the paper copy of the same invoice number show different "
                  "amounts or sellers, so one of them is wrong; held until checked against the paper and the seller",
         "url": MEDIA_RULE_URL},
        CITE_ART67,
    ],
}
RULES["PLATFORM_RECORD_CONFLICT"] = {
    "title": "Hold: the same invoice appears twice in the platform downloads with different amounts",
    "severity": "hold",
    "citations": [
        {"label": "VAT Act Art. 51(1)(5): filing both records would claim the invoice's input tax twice; the downloads "
                  "disagree (for example a later download after the seller corrected the invoice), so a person picks "
                  "the right one", "url": LAW + "G0340080&flno=51"},
    ],
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
    doc_kind: str | None = None          # transaction_detail | payment_slip | utility_bill | None, from printed keywords
    small_business_receipt: bool = False   # a plain receipt that prints 免用統一發票 (issuer exempt from uniform invoices)
    uncertain: dict = field(default_factory=dict)   # field -> why it is not confirmed on the page
    tax_voucher_kind: str | None = None    # transport_ticket | customs_payment | None (other_voucher_kind)


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


# Utility bills and payment notices (台電, 自來水, 中華電信 and other telecoms, gas). Public utilities issue e-invoices:
# the buyer's input-tax voucher is that e-invoice in the platform download, and the "已開立發票號碼" printed on the paper
# bill is an earlier period's e-invoice. Two signals are required: a utility's name AND a bill marker, so a shop receipt
# that only mentions 電信 is not caught. OCR mixes simplified characters into a line, so the text is folded to
# traditional characters (_UTIL_FOLD) before matching instead of listing every mixed spelling.
UTILITY_ISSUER_KW = ("台灣電力", "台電", "電力公司", "電費", "電號",
                     "自來水", "水費", "水號", "水務",
                     "中華電信", "電信費", "電信公司", "台灣大哥大", "遠傳", "亞太電信", "台灣之星",
                     "天然氣", "瓦斯")
UTILITY_BILL_KW = ("繳費通知", "繳款通知", "繳費憑證", "繳費證明", "繳費收據", "繳費單", "應繳總金額", "應繳金額",
                   "本期應繳", "繳費期限", "繳費截止", "繳納期限", "計費期間", "計費期", "用電期間", "用水期間",
                   "用戶編號", "用戶號碼", "電號", "水號", "抄表", "度數", "本期電費", "本期水費", "代收")
_UTIL_FOLD = str.maketrans("臺电费缴应计间户编号华传亚气湾来务单额总资讯话线网帐账发证联统业时数电纳缴",
                           "台電費繳應計間戶編號華傳亞氣灣來務單額總資訊話線網帳帳發證聯統業時數電納繳")
# the words that keep a slip an invoice in voucher_kind (證明聯, 統一發票 ...), folded the same way
UTILITY_PROOF_KW = tuple(sorted({k.translate(_UTIL_FOLD) for k in INVOICE_PROOF_KW}))


def utility_bill(text: str) -> bool:
    """A utility bill / payment notice from printed words (OCR text and / or the read fields). A slip that prints the
    words of an invoice proof (INVOICE_PROOF_KW: 證明聯, 統一發票) is an invoice, never a bill, as in voucher_kind."""
    t = (text or "").replace(" ", "").replace("　", "").translate(_UTIL_FOLD)
    if any(k in t for k in UTILITY_PROOF_KW):
        return False
    issuer = [k for k in UTILITY_ISSUER_KW if k in t]
    marker = [k for k in UTILITY_BILL_KW if k in t]
    # 電號 / 水號 count once: as the issuer signal or as the bill marker, not both
    return bool(issuer) and bool(marker) and len(set(issuer) | set(marker)) >= 2


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
SMALL_RECEIPT_KW = ("免用統一發票", "免用统一發票", "免用統一发票", "免用统一发票")


def small_business_receipt(ocr_text: str) -> bool:
    """A receipt printed 免用統一發票: its issuer does not issue uniform invoices (a small business). Art. 67(3) caps these."""
    return any(k in ocr_text.replace(" ", "") for k in SMALL_RECEIPT_KW)


# Enforcement Rules Art. 38(1)(10): tickets of 火（汽）車 (train or bus), 高鐵, 船舶, 飛機 bought for an employee's business
# trip. Taxis are not on the list (a taxi is not taxed like other transport operators): a taxi receipt is never a ticket.
TRANSPORT_KW = ("台灣高鐵", "臺灣高鐵", "台湾高铁", "高鐵", "高铁", "THSR",
                "臺鐵", "台鐵", "台铁", "臺灣鐵路", "台灣鐵路", "鐵路局", "铁路局", "火車票", "火车票",
                "客運", "客运", "國光號", "国光号",
                "航空", "登機證", "登机证", "機票", "机票", "BOARDINGPASS",
                "船票", "客輪", "客轮", "渡輪", "渡轮", "輪船", "轮船")
# Words that only appear on a ticket itself (not on a receipt from a shop that is near or inside a station).
TICKET_STRONG = ("火車票", "火车票", "登機證", "登机证", "機票", "机票", "船票", "BOARDINGPASS")
TICKET_MARKERS = ("票價", "票价", "票號", "票号", "購票", "购票", "車次", "车次", "座位", "車廂", "车厢", "乘車日期",
                  "乘车日期", "全票", "半票", "敬老票", "愛心票", "對號", "自由座", "航班", "班次", "艙等", "舱等",
                  "起站", "訖站", "起訖", "去程", "回程", "單程", "单程", "來回", "来回", "登機", "登机")
_ROUTE = re.compile(r"[一-鿿]{2,4}(?:→|->|－|—|~|～|-)[一-鿿]{2,4}")
TAXI_KW = ("計程車", "计程车", "TAXI", "車隊", "车队", "叫車", "叫车")
# 海關代徵營業稅繳納證: the customs office collects VAT on imports and issues a payment certificate (format code 28)
CUSTOMS_KW = ("代徵", "代征", "營業稅繳納證", "营业税缴纳证", "繳納證", "缴纳证")


def other_voucher_kind(text: str, doc_type: str | None = None) -> str | None:
    """transport_ticket or customs_payment from printed words (OCR text and / or the read fields), else None.
    A uniform invoice stays a uniform invoice: an e-invoice from the railway keeps format code 25."""
    t = (text or "").replace(" ", "").replace("　", "").upper()
    if ("海關" in t or "海关" in t) and any(k in t for k in CUSTOMS_KW):
        return "customs_payment"
    if doc_type not in (None, "receipt"):
        return None
    if any(k in t for k in TAXI_KW):
        return None
    if any(k.upper() in t for k in TICKET_STRONG):
        return "transport_ticket"
    # A carrier's name alone is not a ticket: a shop in a station or an airport prints it in its address. The text must
    # also look like a ticket (fare, seat, route, travel date...).
    if any(k.upper() in t for k in TRANSPORT_KW) and (any(k.upper() in t for k in TICKET_MARKERS)
                                                      or _ROUTE.search(t)):
        return "transport_ticket"
    return None


def fields_text(f: dict) -> str:
    """Printed words the reader kept in the fields (seller, items, note): used when the OCR lines are not at hand."""
    return " ".join([f.get("seller_name") or "", f.get("note") or ""]
                    + [i.get("desc") or "" for i in f.get("items") or []])


def transport_input_tax(total: int | None) -> int:
    """Art. 38(4): input tax on a two-part register invoice or a transport ticket = total x 5% / (1 + 5%), rounded."""
    return int(round(total * 5 / 105)) if total else 0


FIXED_ASSET_MIN_NET = 80_000   # Audit Regulations Art. 77-1: over NT$80,000 and a useful life over two years
# Items that are usually bought to last more than two years. Repairs, rentals and consumables of the same equipment
# are expenses, so a line that also names one of FIXED_ASSET_NOT is not hinted.
FIXED_ASSET_KW = ("電腦", "电脑", "筆電", "笔电", "筆記型電腦", "伺服器", "服务器", "工作站", "影印機", "影印机", "事務機",
                  "事务机", "印表機", "打印机", "投影機", "投影机", "冷氣機", "冷气机", "空調", "空调", "冷凍櫃", "冷冻柜",
                  "冰箱", "機台", "机台", "機器", "机器", "機械", "机械", "設備", "设备", "生財器具", "辦公桌", "办公桌",
                  "辦公椅", "貨架", "货架", "堆高機", "发电机", "發電機", "貨車", "货车", "监视系统", "監視系統")
FIXED_ASSET_NOT = ("維修", "维修", "保養", "保养", "修理", "租", "耗材", "碳粉", "墨水", "滑鼠", "鍵盤", "清洗", "清潔",
                   "運費", "运费", "授權", "月費", "年費", "線材", "配件")


def fixed_asset_reason(net: int | None, item_texts: list[str]) -> str | None:
    """Why a line may be a fixed asset (deduction code 2 / 4), or None. Only a hint: the code is never changed."""
    if net is not None and net >= FIXED_ASSET_MIN_NET:
        return f"net NT${net:,} >= NT${FIXED_ASSET_MIN_NET:,}"
    for desc in item_texts:
        if not desc or any(k in desc for k in FIXED_ASSET_NOT):
            continue
        for kw in FIXED_ASSET_KW:
            if kw in desc:
                return f"item '{desc}' (keyword '{kw}')"
    return None


def net_amount(net: int | None, tax: int | None, total: int | None) -> int | None:
    if net is not None:
        return net
    if total is not None:
        return total - (tax or 0)
    return None


def prints_invoice_ids(d: Doc) -> bool:
    """Whether this document is supposed to print an invoice number and the seller's UBN: a uniform invoice
    (e-invoice proof, triplicate, two-part register), or a document of unknown type that carries an invoice number.
    Plain receipts (a taxi receipt prints neither) and payment slips / transaction details (held as NOT_A_VOUCHER)
    are not."""
    if d.doc_kind:
        return False
    return d.doc_type in TAX_VOUCHERS or (d.doc_type is None and bool(d.invoice_no))


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
    if d.tax_voucher_kind == "transport_ticket":     # Art. 38(1)(10): no buyer UBN on a ticket; 5/105 of the total
        return transport_input_tax(d.total)
    if d.tax_voucher_kind == "customs_payment":      # the importer on the certificate must be this client
        return (d.tax or 0) if d.buyer_ubn in (None, client["ubn"]) else 0
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

def _platform_set(platform_invoices, client_id: str) -> set[str]:
    if not platform_invoices:
        return set()
    if isinstance(platform_invoices, dict):
        return set(platform_invoices.get(client_id) or ())
    return set(platform_invoices)


def _platform_list(platform_records, client_id: str) -> list[dict]:
    if not platform_records:
        return []
    if isinstance(platform_records, dict):
        return list(platform_records.get(client_id) or ())
    return list(platform_records)


def utility_platform_match(d: Doc, records: list[dict]) -> dict | None:
    """The platform input record for this utility bill: an issued uniform invoice from the same seller UBN with the same
    total (amount + tax, or the amount when it already includes tax). The records are the client's filing period."""
    if not d.seller_ubn or d.total is None:
        return None
    for r in records:
        if r.get("side", "input") != "input" or r.get("status") != "issued" or r.get("seller_ubn") != d.seller_ubn:
            continue
        total = r["amount"] if r.get("tax_inclusive") else (r.get("amount") or 0) + (r.get("tax") or 0)
        if total == d.total:
            return r
    return None


def _utility_exception(d: Doc, records: list[dict], platform_loaded: bool) -> "Exception_":
    head = ("Utility bill or payment notice (electricity, water, telecom or gas). Book the input tax from the utility's "
            "e-invoice in the client's e-invoice platform download; keep this bill as proof of payment and do not key "
            "it. The invoice number printed on the bill belongs to an earlier period's e-invoice and is not used.")
    m = utility_platform_match(d, records)
    if m:
        tail = (f" Matched: platform e-invoice {m.get('invoice_no') or m.get('other_voucher_no') or ''} "
                f"({m.get('month')}), same seller UBN and total.")
    elif platform_loaded:
        tail = " Not yet matched: no platform record with the same seller UBN and total; check the platform download."
    else:
        tail = " Not yet matched: check the platform download for this bill's e-invoice."
    return Exception_(d.doc_id, d.client_id, "UTILITY_BILL_PLATFORM", "info", head + tail, ["seller_name", "total"])


def run_rules(docs: list[Doc], clients: dict[str, dict], registry=None,
              platform_invoices: set[str] | dict[str, set[str]] | None = None,
              platform_void: set[str] | dict[str, set[str]] | None = None,
              platform_records: list[dict] | dict[str, list[dict]] | None = None) -> list[Exception_]:
    """registry: optional ledger_room.registry.Registry (local copy of the MOF business-registration file).
    With it, a checksum failure that is one digit away from a registered business is reported as a likely misread,
    and a well-formed number missing from the register gets a UBN_NOT_REGISTERED warning.
    platform_invoices: invoice numbers already in the e-invoice platform download for this period, either one set
    (every client) or {client_id: set}. A photographed voucher whose number is in it gets IN_PLATFORM (info) and the
    export leaves it out of the file (NOT_IN_FILE), so the same input tax is not claimed twice. Default: none.
    platform_void: invoice numbers the platform download marks 作廢 (einvoice_platform.platform_void_numbers), same
    shapes; a photographed voucher with one of them gets VOID_ON_PLATFORM (high) and is held. Default: none.
    platform_records: the platform download's input records for the client's period (einvoice_platform.load_client(...)
    ["input"]), one list or {client_id: list}; only used to say whether a utility bill's e-invoice is already there."""
    out: list[Exception_] = []

    for d in docs:
        c = clients[d.client_id]
        # 00. a utility bill is not keyed: its input tax comes from the utility's e-invoice in the platform download.
        #     No other per-document rule runs on it (its fields never reach the filing), so it does not go to a person.
        if d.doc_kind == "utility_bill":
            out.append(_utility_exception(d, _platform_list(platform_records, d.client_id),
                                          platform_records is not None))
            continue
        # 0b. the platform download marks this invoice number as voided
        if d.invoice_no and d.invoice_no in _platform_set(platform_void, d.client_id):
            out.append(Exception_(d.doc_id, d.client_id, "VOID_ON_PLATFORM", "high",
                                  f"Invoice {d.invoice_no} is voided on the e-invoice platform: do not claim it; ask the "
                                  "client for the replacement invoice.", ["invoice_no"]))
        # 0a. the e-invoice platform download already has this invoice for this client (imported on its own)
        if d.invoice_no and d.invoice_no in _platform_set(platform_invoices, d.client_id):
            out.append(Exception_(d.doc_id, d.client_id, "IN_PLATFORM", "info",
                                  f"Invoice {d.invoice_no} is already in the platform download: not entered again.",
                                  ["invoice_no"]))
        # 0. a total that two independent readings do not agree on is checked by a person before anything is filed
        if d.uncertain.get("total"):
            out.append(Exception_(d.doc_id, d.client_id, "READ_REVIEW", "review",
                                  f"Total not confirmed on the page (read NT${d.total:,}); check it by eye." if d.total
                                  else "Total not found on the page; fill it in by eye.", ["total"], confidence="low"))
        # 0a'. strict reading (pipeline.STRICT): invoice number, date or voucher type without two agreeing sources
        for k, label in (("invoice_no", "Invoice number"), ("date", "Date")):
            if d.uncertain.get(k):
                out.append(Exception_(d.doc_id, d.client_id, "READ_REVIEW", "review",
                                      f"{label} read from one source only ({d.uncertain[k]}); check it by eye.", [k],
                                      confidence="low"))
        if d.uncertain.get("doc_type"):
            out.append(Exception_(d.doc_id, d.client_id, "READ_REVIEW", "review",
                                  "Voucher type 'receipt' comes from the model alone and no invoice number was read: "
                                  "check whether it is a uniform invoice.", ["doc_type", "invoice_no"], confidence="low"))
        # 0b. a uniform invoice always prints its number and the seller's UBN: a blank one goes to a person
        if prints_invoice_ids(d) and not d.invoice_no:
            out.append(Exception_(d.doc_id, d.client_id, "READ_REVIEW", "review",
                                  "Invoice number not found on the page; fill it in by eye.", ["invoice_no"],
                                  confidence="low"))
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
                if role == "seller" and prints_invoice_ids(d):
                    out.append(Exception_(d.doc_id, d.client_id, "READ_REVIEW", "review",
                                          "Seller UBN not found on the page; fill it in by eye.", ["seller_ubn"],
                                          confidence="low"))
                continue
            if role == "seller" and v == c.get("ubn"):
                # the client does not issue its own purchase vouchers: its number was most likely read from the buyer
                # box or the company letterhead, and the real seller UBN was lost
                out.append(Exception_(d.doc_id, d.client_id, "READ_REVIEW", "review",
                                      f"Seller UBN {v} is this client's own UBN: seller and buyer were probably swapped "
                                      "when reading. Check both UBNs by eye.", ["seller_ubn", "buyer_ubn"],
                                      confidence="low"))
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
            elif not d.buyer_ubn and d.doc_type == "two_part_register":
                out.append(Exception_(d.doc_id, d.client_id, "BUYER_NOT_CLIENT", "medium",
                                      "Two-part invoice (二聯式) without the buyer's UBN: its input tax cannot be deducted. "
                                      "Only a two-part cash-register invoice that carries the buyer's UBN is a deductible "
                                      "voucher (format code 22).", ["buyer_ubn"], citations=CITE_TWO_PART))
            elif not d.buyer_ubn:
                out.append(Exception_(d.doc_id, d.client_id, "BUYER_NOT_CLIENT", "warning",
                                      "No buyer UBN on the voucher (consumer-style invoice): an expense voucher only, "
                                      "no deductible input tax.", ["buyer_ubn"]))

        # 7a. other vouchers that carry input tax (Enforcement Rules Art. 38(1))
        if d.tax_voucher_kind == "customs_payment":
            out.append(Exception_(d.doc_id, d.client_id, "CUSTOMS_KEY_BY_HAND", "hold",
                                  "Customs-collected VAT payment certificate (海關代徵營業稅繳納證, format code 28). Key the "
                                  "14-character 繳納證號碼, the 營業稅稅基 and the tax by hand from the deduction copy, and "
                                  "confirm the importer (納稅義務人) is this client.", ["invoice_no", "net", "tax"]))
        elif d.tax_voucher_kind == "transport_ticket":
            t = transport_input_tax(d.total)
            out.append(Exception_(d.doc_id, d.client_id, "TRANSPORT_TICKET", "warning",
                                  "Transport ticket (載有稅額之其他憑證, format code 22): input tax "
                                  + (f"NT${t:,} = NT${d.total:,} x 5 / 105" if d.total else "= total x 5 / 105 (total not read)")
                                  + ". Confirm this was an employee's business trip; otherwise it is not deductible.",
                                  ["total"]))
            if not d.seller_ubn and not d.uncertain.get("seller_ubn"):
                out.append(Exception_(d.doc_id, d.client_id, "READ_REVIEW", "review",
                                      "Carrier's UBN not found on the ticket; key it in (the media file needs the "
                                      "seller's UBN).", ["seller_ubn"], confidence="low"))
        elif (d.doc_type in (None, "receipt") and not d.doc_kind and d.tax and d.buyer_ubn
              and d.buyer_ubn == c["ubn"]):
            what = "Receipt" if d.doc_type == "receipt" else "Voucher of unrecognised type"
            out.append(Exception_(d.doc_id, d.client_id, "RECEIPT_TAX_UNVERIFIED", "hold",
                                  f"{what} shows a tax amount (NT${d.tax:,}): confirm it is a voucher kind the MOF allows "
                                  "as 載有稅額之其他憑證 before claiming input tax.", ["tax", "buyer_ubn"]))

        # 7b. slips that are not vouchers
        if d.tax_voucher_kind:
            pass   # a ticket or a customs certificate prints neither an invoice number nor (always) a seller UBN
        elif d.doc_kind or (d.doc_type in (None, "receipt") and not d.invoice_no and not d.seller_ubn):
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

        # 4b. fixed assets (401: purchases and expenses vs fixed assets; deduction code 2 / 4). A hint, never a change.
        if not d.doc_kind:
            itax = claimable_input_tax(d, c, allow_presumed=True)
            fa = fixed_asset_reason(net_amount(d.net, d.tax if d.tax is not None else (itax or None), d.total),
                                    [i.desc for i in d.items])
            if fa:
                out.append(Exception_(d.doc_id, d.client_id, "FIXED_ASSET_HINT", "warning",
                                      f"May be a fixed asset ({fa}): confirm deduction code 2 (or 4 if non-deductible). "
                                      "Code left unchanged.", ["net", "items"]))

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

    # 9. Audit Regulations Art. 67(3): plain receipts issued by small businesses (printed 免用統一發票), cumulative per
    #    client, capped at 3% of the year's manufacturing expenses + operating expenses. Off unless both figures are
    #    configured per client ("annual_manufacturing_expenses", "annual_operating_expenses"; a trading or service
    #    company with no manufacturing sets the first to 0).
    run_sum: dict[str, int] = defaultdict(int)
    for d in sorted(docs, key=lambda x: (x.client_id, x.date or date.min, x.doc_id)):
        c = clients[d.client_id]
        mfg, opx = c.get("annual_manufacturing_expenses"), c.get("annual_operating_expenses")
        if mfg is None or opx is None or not (mfg + opx):
            continue
        if d.doc_type != "receipt" or not d.small_business_receipt or d.tax or d.total is None:
            continue
        run_sum[d.client_id] += d.total
        cap = round((mfg + opx) * 0.03)
        if run_sum[d.client_id] > cap:
            out.append(Exception_(d.doc_id, d.client_id, "SMALL_RECEIPT_CAP", "warning",
                                  f"Small-business plain receipts so far NT${run_sum[d.client_id]:,} exceed 3% of the "
                                  f"client's manufacturing + operating expenses (NT${mfg:,} + NT${opx:,}, cap "
                                  f"NT${cap:,}); the excess is not recognised as an expense. The base should be the "
                                  "amounts the tax office assesses; the configured figures are estimates.", ["total"]))

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
        if d.doc_kind == "utility_bill":
            continue   # not keyed; its printed invoice number is an earlier period's e-invoice (UTILITY_BILL_PLATFORM)
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
        if d.date and d.total and d.total >= 100 and d.doc_kind != "utility_bill":
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
            why = (" If this client deducts its input tax twice, that may constitute over-claimed input tax (VAT Act Art. 51(1)(5))." if tax > 0 else
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
