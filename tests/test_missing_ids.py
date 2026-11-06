"""A blank invoice number or seller UBN on a uniform invoice goes to a person (READ_REVIEW), like an unconfirmed total.

Found on the first real_tw run: e-invoice photos with no invoice number or no seller UBN read went through without a
hold. Kinds that do not print these fields (plain receipts, payment slips, transaction details) are not flagged.
"""
from datetime import date

from ledger_room import rules

CL = {"A": {"id": "A", "name": "甲公司", "ubn": "00501503", "period": ["2026-07-01", "2026-10-31"], "policy": {}}}
SELLER = "04595257"


def doc(**kw):
    base = dict(doc_type="e_invoice", invoice_no="LF16191539", date=date(2026, 9, 3), seller_ubn=SELLER, total=120)
    base.update(kw)
    return rules.Doc("x", "A", "2026-09", **base)


def reviews(d):
    return [e for e in rules.run_rules([d], CL) if e.rule_id == "READ_REVIEW"]


def test_complete_invoice_gets_no_review():
    assert reviews(doc()) == []


def test_blank_invoice_no_goes_to_a_person():
    rr = reviews(doc(invoice_no=None))
    assert [(e.evidence_fields, e.severity) for e in rr] == [(["invoice_no"], "review")]
    assert "Invoice number not found" in rr[0].message


def test_blank_seller_ubn_goes_to_a_person():
    for t in ("e_invoice", "triplicate_invoice", "two_part_register"):
        rr = reviews(doc(doc_type=t, seller_ubn=None))
        assert [(e.evidence_fields, e.severity) for e in rr] == [(["seller_ubn"], "review")], t


def test_both_blank_gives_two_reviews_and_no_evaluated_issue():
    ex = rules.run_rules([doc(invoice_no=None, seller_ubn=None)], CL)
    assert sorted(f for e in ex if e.rule_id == "READ_REVIEW" for f in e.evidence_fields) == ["invoice_no", "seller_ubn"]
    assert rules.issues_by_doc(ex) == {}            # a routing note, not one of the six evaluated issue types
    assert "READ_REVIEW" not in rules.ISSUE_TYPES


def test_unconfirmed_seller_ubn_is_not_reported_twice():
    rr = reviews(doc(seller_ubn=None, uncertain={"seller_ubn": "model-only"}))
    assert len(rr) == 1 and rr[0].evidence_fields == ["seller_ubn"] and "not confirmed" in rr[0].message


def test_unknown_type_with_invoice_number_needs_seller_ubn():
    rr = reviews(doc(doc_type=None, seller_ubn=None))
    assert [e.evidence_fields for e in rr] == [["seller_ubn"]]


def test_kinds_without_these_fields_are_not_flagged():
    # a plain receipt (e.g. a taxi receipt) prints neither an invoice number nor a seller UBN
    assert reviews(doc(doc_type="receipt", invoice_no=None, seller_ubn=None)) == []
    # a small-business receipt with a UBN stamp but no invoice number
    assert reviews(doc(doc_type="receipt", invoice_no=None, small_business_receipt=True)) == []
    # payment slips and transaction details are held as NOT_A_VOUCHER instead
    for kind in ("payment_slip", "transaction_detail"):
        ex = rules.run_rules([doc(invoice_no=None, seller_ubn=None, doc_kind=kind)], CL)
        assert not any(e.rule_id == "READ_REVIEW" for e in ex), kind
        assert any(e.rule_id == "NOT_A_VOUCHER" for e in ex), kind
    # unknown type with neither number: NOT_A_VOUCHER, not a review
    ex = rules.run_rules([doc(doc_type=None, invoice_no=None, seller_ubn=None)], CL)
    assert [e.rule_id for e in ex if e.severity in ("hold", "review")] == ["NOT_A_VOUCHER"]
