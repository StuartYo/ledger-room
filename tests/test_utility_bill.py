"""Utility bills and payment notices (台電, 自來水, telecoms, gas): recognised from printed words, never keyed from paper.
All OCR text below is SYNTHETIC (made-up numbers and addresses)."""
from datetime import date

import pytest

from ledger_room import export, pipeline, rules
from ledger_room import voucher_export as ve

CL = {"A": {"id": "A", "name": "甲公司", "ubn": "00501503", "period": ["2026-09-01", "2026-10-31"], "policy": {"any": 500}}}
TAXREG = {"00501503": "123456789"}

ELECTRIC = ("台灣電力公司 繳費通知單 電號 01-23-4567-89 計費期間 115/08/05至115/10/04 計費度數 812 "
            "應繳總金額 3,245 繳費期限 115/10/20 已開立電子發票 發票號碼 AB12345678")
WATER = "台灣自來水公司 水費 繳費通知 水號 9-87-654321-0 用水期間 115/07/01-115/08/31 應繳金額 612"
TELECOM = "中華電信 電信費 繳費通知 用戶號碼 02-2345-6789 本期應繳 1,399 繳費截止 115/10/15"
GAS = "大台北區瓦斯 天然氣 繳費通知單 用戶編號 1234567 應繳金額 880 抄表日 115/09/30"
SIMPLIFIED = "台湾电力公司 缴费通知单 电号 01-23-4567-89 应缴总金额 3,245 缴费期限 115/10/20"
MIXED = "台灣電力公司 缴費通知單 電号 01-23-4567-89 應缴總金額 3,245"


@pytest.mark.parametrize("text", [ELECTRIC, WATER, TELECOM, GAS, SIMPLIFIED, MIXED])
def test_utility_bills_are_recognised(text):
    assert rules.utility_bill(text)


@pytest.mark.parametrize("text", [
    "中華電信門市 手機殼 390 現金 400 找零 10",                            # a shop that mentions 電信: no bill marker
    "遠傳電信 加盟門市 充電線 590",
    "地價稅繳款書 繳納期限 115/11/30 應繳金額 4,321",                         # a tax bill: bill words, no utility
    "台灣電力公司 電子發票證明聯 115年09-10月 計費期間 電號 01-23-4567-89",   # the utility's e-invoice proof
    "台湾电力公司 电子发票证明联 115年09-10月 应缴总金额 3,245",               # same, simplified OCR
    "台灣自來水公司 統一發票 水費 計費期間 115/07-115/08",                    # prints 統一發票: an invoice
    "",
])
def test_not_utility_bills(text):
    assert not rules.utility_bill(text)


def test_a_collection_slip_for_a_telecom_bill_counts_as_a_bill():
    # a convenience-store collection slip (代收) for a telecom bill is payment proof; the e-invoice is still the voucher
    assert rules.utility_bill("便利商店 代收 中華電信 電信費 1,399")


def _doc(text, **fields):
    f = {"doc_type": "e_invoice", "invoice_no": "AB12345678", "date": "2026-10-05", "seller_name": "台灣電力公司",
         "seller_ubn": None, "buyer_ubn": "00501503", "total": 3245, "tax": None, "net": None, "items": [], **fields}
    lines = [{"id": "L1", "text": text, "box": [0, 0, 100, 20], "conf": 0.9}]
    return f, pipeline.to_rule_doc({"doc_id": "bill", "client_id": "A", "month": "2026-10"}, f, {"total": "model-only"},
                                   lines)


def test_bill_gets_its_own_kind_and_no_invoice_number():
    f, d = _doc(ELECTRIC)
    assert d.doc_kind == "utility_bill"
    assert d.invoice_no is None          # the printed number is an earlier period's e-invoice
    _, proof = _doc("台灣電力公司 電子發票證明聯 計費期間 電號 01-23-4567-89")
    assert proof.doc_kind is None and proof.invoice_no == "AB12345678"


def test_bill_is_not_held_and_not_sent_to_a_person():
    _, d = _doc(ELECTRIC)                # total unconfirmed, no seller UBN, over the 500 approval limit
    ex = rules.run_rules([d], CL)
    assert [e.rule_id for e in ex] == ["UTILITY_BILL_PLATFORM"]
    e = ex[0]
    assert e.severity == "info" and e.severity not in ("hold", "review")
    assert "platform" in e.message and "do not key" in e.message and "Not yet matched" in e.message
    assert any(c["url"].endswith("G0340080&flno=33") for c in e.to_dict()["citations"])


def test_bill_is_not_in_the_duplicate_check():
    _, b1 = _doc(ELECTRIC)
    _, b2 = _doc(ELECTRIC)
    b2.doc_id = "bill2"
    # the utility's e-invoice proof photographed too, with the number the bill printed and the same date and total
    inv = rules.Doc("inv", "A", "2026-10", doc_type="e_invoice", invoice_no="AB12345678", date=date(2026, 10, 5),
                    seller_ubn="12345675", buyer_ubn="00501503", net=3090, tax=155, total=3245)
    ex = rules.run_rules([b1, b2, inv], CL)
    assert not any(e.rule_id.startswith("DUP_") for e in ex)


def _result(d, f):
    ex = [e.to_dict() for e in rules.run_rules([d], CL)]
    return {"clients": list(CL.values()), "exceptions": ex,
            "docs": [{"doc_id": d.doc_id, "client_id": "A", "fields": f, "tax_voucher_kind": None,
                      "small_business_receipt": False}]}


def test_bill_never_becomes_an_input_line():
    f, d = _doc(ELECTRIC, net=3090, tax=155)
    res = _result(d, f)
    row = export.rows(res)[0]
    assert (row["status"], row["format_code_格式代號"], row["invoice_no"], row["tax"]) == ("NOT_IN_FILE", "", "", "")
    for hold in (False, True):
        recs, _ = export.media_records(res, TAXREG, include_hold=hold)
        assert recs == {}
    assert ve.rows(res) == []


def test_bill_matched_with_a_platform_record():
    f, d = _doc(ELECTRIC, seller_ubn="12345675")
    rec = {"side": "input", "status": "issued", "seller_ubn": "12345675", "amount": 3090, "tax": 155,
           "tax_inclusive": False, "invoice_no": "CD87654321", "month": "2026-10"}
    ex = rules.run_rules([d], CL, platform_records={"A": [rec]})
    assert "Matched: platform e-invoice CD87654321" in ex[0].message and ex[0].severity == "info"
    other = {**rec, "amount": 3000}
    ex = rules.run_rules([d], CL, platform_records={"A": [other]})
    assert "Not yet matched" in ex[0].message and "no platform record" in ex[0].message
