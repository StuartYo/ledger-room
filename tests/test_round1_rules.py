"""Citations follow the voucher, prior-period vouchers are warnings, registry lookup, 401 export codes."""
from datetime import date

from ledger_room import export, rules
from ledger_room.registry import Registry

CL = {"A": {"id": "A", "name": "甲公司", "ubn": "00501503", "period": ["2026-09-01", "2026-10-31"], "policy": {}},
      "B": {"id": "B", "name": "乙公司", "ubn": "12345675", "period": ["2026-09-01", "2026-10-31"], "policy": {}}}


def taxi(cid, doc):
    return rules.Doc(doc, cid, "2026-10", doc_type="receipt", date=date(2026, 10, 12), seller_ubn="85927754",
                     items=[rules.Item("計程車資", 1, 550)], total=550)


def test_taxi_duplicate_cites_income_tax_not_vat():
    ex = [e for e in rules.run_rules([taxi("A", "a"), taxi("B", "b")], CL) if e.rule_id == "DUP_CROSS_CLIENT"]
    assert len(ex) == 2
    for e in ex:
        urls = [c["url"] for c in e.to_dict()["citations"]]
        assert any("G0340003&flno=24" in u for u in urls) and any("G0340051&flno=67" in u for u in urls)
        assert not any("flno=51" in u for u in urls)


def test_invoice_duplicate_cites_vat_art51():
    def d(doc):
        return rules.Doc(doc, "A", "2026-10", doc_type="triplicate_invoice", invoice_no="AB12345678",
                         date=date(2026, 9, 3), seller_ubn="85927754", buyer_ubn="00501503", tax=50, total=1050)
    ex = [e for e in rules.run_rules([d("x"), d("y")], CL) if e.rule_id == "DUP_CROSS_MONTH"]
    assert ex and any("flno=51" in c["url"] for c in ex[0].to_dict()["citations"])


def test_prior_period_is_a_warning_but_later_is_not():
    early = rules.Doc("e", "A", "2026-09", doc_type="receipt", date=date(2026, 8, 30), total=100)
    late = rules.Doc("l", "A", "2026-10", doc_type="receipt", date=date(2026, 11, 3), total=100)
    sev = {e.doc_id: e.severity for e in rules.run_rules([early, late], CL) if e.rule_id == "PERIOD_MISMATCH"}
    assert sev == {"e": "warning", "l": "medium"}


def test_registry_separates_misread_from_unregistered(tmp_path):
    p = tmp_path / "reg.csv"
    p.write_text("營業地址,統一編號,營業人名稱\n示範路,00501503,示範甲\n示範路,85927754,示範乙\n", encoding="utf-8")
    reg = Registry.from_csv(p)
    assert len(reg) == 2
    misread = rules.Doc("m", "B", "2026-10", seller_ubn="00501502")       # one digit off a registered number
    ex = rules.run_rules([misread], CL, registry=reg)
    assert [e.rule_id for e in ex] == ["UBN_CHECKSUM"] and "00501503" in ex[0].message
    own = rules.Doc("o", "B", "2026-10", seller_ubn="12345675")           # the client's own UBN is not looked up
    assert rules.run_rules([own], CL, registry=reg) == []
    unknown = rules.Doc("u", "A", "2026-10", seller_ubn="12345675")       # valid, but not in this register
    assert [e.rule_id for e in rules.run_rules([unknown], CL, registry=reg)] == ["UBN_NOT_REGISTERED"]
    assert "UBN_NOT_REGISTERED" not in rules.ISSUE_TYPES                  # never counted in the evaluation


def test_401_export_codes():
    res = {"clients": [CL["A"]], "docs": [
        {"doc_id": "t", "client_id": "A", "fields": {"doc_type": "triplicate_invoice", "buyer_ubn": "00501503",
                                                      "date": "2026-09-03", "tax": 50, "total": 1050}},
        {"doc_id": "g", "client_id": "A", "fields": {"doc_type": "e_invoice", "buyer_ubn": "00501503",
                                                      "date": "2026-09-04", "tax": 40, "total": 840}},
        {"doc_id": "r", "client_id": "A", "fields": {"doc_type": "receipt", "date": "2026-09-05", "total": 550}}],
        "exceptions": [{"doc_id": "g", "rule_id": "S19_NONDEDUCTIBLE", "severity": "medium"}]}
    rows = {r["doc_id"]: r for r in export.rows(res)}
    assert (rows["t"]["format_code_格式代號"], rows["t"]["deduction_code_扣抵代號"], rows["t"]["status"]) == ("21", "1", "READY")
    assert (rows["g"]["format_code_格式代號"], rows["g"]["deduction_code_扣抵代號"]) == ("25", "3")
    assert rows["r"]["status"] == "NOT_IN_401" and rows["t"]["date_roc"] == "1150903"
