"""Payment slips and transaction details: held as non-vouchers and paired with the purchase they belong to."""
from datetime import date

from ledger_room import rules

CL = {"A": {"id": "A", "name": "甲公司", "ubn": "00501503", "period": ["2026-07-01", "2026-10-31"], "policy": {}}}


def test_voucher_kind_reads_simplified_ocr_and_ignores_invoice_proofs():
    assert rules.voucher_kind("奇菜 B4-054-交易明细 DL87432616", "DL87432616") == "transaction_detail"
    assert rules.voucher_kind("多元支付服務 端末代號 82000015 交易類别 付款", None) == "payment_slip"
    assert rules.voucher_kind("電子發票證明聯 交易明細", "AB12345678") is None
    assert rules.voucher_kind("銷貨明細表 發票號碼 FN39952438", "FN39952438") is None


def test_detail_and_payment_slip_are_held_and_paired():
    detail = rules.Doc("d", "A", "2026-10", doc_type="receipt", invoice_no="DL87432616", date=date(2026, 7, 17),
                       total=2495, doc_kind="transaction_detail")
    slip = rules.Doc("s", "A", "2026-10", doc_type="e_invoice", date=date(2026, 7, 17), seller_ubn="82000015",
                     total=2495, doc_kind="payment_slip")
    ex = rules.run_rules([detail, slip], CL)
    held = {e.doc_id for e in ex if e.rule_id == "NOT_A_VOUCHER"}
    dup = {e.doc_id: e.severity for e in ex if e.rule_id.startswith("DUP_")}
    assert held == {"d", "s"}
    assert set(dup) == {"s"} and dup["s"] == "warning"
    assert not any(e.rule_id == "UBN_CHECKSUM" for e in ex)   # the terminal id on the slip is not a UBN


def test_slip_without_any_number_is_held_even_if_type_unknown():
    d = rules.Doc("x", "A", "2026-10", doc_type=None, date=date(2026, 8, 30), total=2651)
    assert any(e.rule_id == "NOT_A_VOUCHER" for e in rules.run_rules([d], CL))
