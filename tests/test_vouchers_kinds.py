"""Voucher kinds and 401 details from the bookkeeping-practice review (gap rows G4, G5, G6, G8, G10, G15, G19 and the
platform-download hook). All documents are SYNTHETIC (tests/fixtures/vouchers/synthetic_ocr.json)."""
import csv
import json
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from eval.media_file_check import check_file, check_record
from ledger_room import export, pipeline, rules

FIX = json.loads((Path(__file__).parent / "fixtures" / "vouchers" / "synthetic_ocr.json").read_text(encoding="utf-8"))
CLIENT = FIX["client"]
CL = {"A": CLIENT, "B": {"id": "B", "name": "乙公司", "ubn": "12345675", "period": ["2026-09-01", "2026-10-31"],
                         "policy": {}}}
TAXREG = {CLIENT["ubn"]: "123456789"}      # synthetic 稅籍編號


def ocr_lines(name):
    return [{"id": f"L{i}", "text": t, "box": [0, i * 20, 100, i * 20 + 18], "conf": 0.9}
            for i, t in enumerate(FIX["docs"][name]["ocr"])]


def rule_doc(name, client="A"):
    f = FIX["docs"][name]["fields"]
    return pipeline.to_rule_doc({"doc_id": name, "client_id": client, "month": "2026-10"}, f, {}, ocr_lines(name))


def result_for(names, exceptions=None, client="A", **extra_doc):
    """A result dict as pipeline.audit writes it, with the rules run on the synthetic docs."""
    rdocs = [rule_doc(n, client) for n in names]
    exc = exceptions if exceptions is not None else [e.to_dict() for e in rules.run_rules(rdocs, CL)]
    docs = [{"doc_id": n, "client_id": client, "fields": FIX["docs"][n]["fields"],
             "tax_voucher_kind": r.tax_voucher_kind, "small_business_receipt": r.small_business_receipt, **extra_doc}
            for n, r in zip(names, rdocs)]
    return {"clients": list(CL.values()), "docs": docs, "exceptions": exc}


def ids_of(exs, doc):
    return {e["rule_id"] for e in exs if e["doc_id"] == doc}


# ---- G6: a receipt that prints a tax amount is not automatically 載有稅額之其他憑證 ------------------------------------
def test_receipt_with_tax_is_held_with_art38_citation():
    res = result_for(["receipt_with_tax"])
    e = next(e for e in res["exceptions"] if e["rule_id"] == "RECEIPT_TAX_UNVERIFIED")
    assert e["severity"] == "hold"
    assert "confirm it is a voucher kind the MOF allows as 載有稅額之其他憑證 before claiming input tax" in e["message"]
    assert any(c["url"].endswith("G0340081&flno=38") for c in e["citations"])
    row = export.rows(res)[0]
    assert (row["status"], row["format_code_格式代號"]) == ("HOLD", "22")
    assert "載有稅額之其他憑證" in row["note"] and "G0340081&flno=38" in row["note"]
    recs, _ = export.media_records(res, TAXREG)
    assert recs == {}                                     # never written as a READY line


def test_receipt_with_tax_is_held_even_without_the_exception():
    """An older result.json without the new exception still exports the receipt as HOLD."""
    row = export.rows(result_for(["receipt_with_tax"], exceptions=[]))[0]
    assert row["status"] == "HOLD"


def test_two_part_register_invoice_keeps_format_22_ready():
    res = {"clients": [CLIENT], "exceptions": [], "docs": [
        {"doc_id": "w", "client_id": "A", "fields": {"doc_type": "two_part_register", "buyer_ubn": "00501503",
                                                      "date": "2026-09-04", "tax": 50, "total": 1050}}]}
    row = export.rows(res)[0]
    assert (row["status"], row["format_code_格式代號"]) == ("READY", "22")


# ---- G5: employees' business-travel tickets (Art. 38(1)(10)) ------------------------------------------------------
@pytest.mark.parametrize("name,kind", [("thsr_ticket", "transport_ticket"), ("tra_ticket_no_ubn", "transport_ticket"),
                                       ("bus_ticket", "transport_ticket"), ("taxi_receipt", None),
                                       ("customs_certificate", "customs_payment"), ("receipt_with_tax", None),
                                       ("small_business_receipt", None)])
def test_voucher_kind_from_ocr_keywords(name, kind):
    assert rule_doc(name).tax_voucher_kind == kind


def test_kind_from_fields_alone_and_uniform_invoices_stay():
    assert rules.other_voucher_kind("台灣高鐵 台北-左營", "receipt") == "transport_ticket"
    assert rules.other_voucher_kind("台灣高鐵 台北-左營", "e_invoice") is None      # an e-invoice keeps format 25
    assert rules.other_voucher_kind("長榮航空 登機證", None) == "transport_ticket"
    assert rules.other_voucher_kind("台鐵 計程車 車資", "receipt") is None           # taxis never count
    assert rules.other_voucher_kind("便利商店 飲料", "receipt") is None


def test_transport_ticket_input_tax_is_5_over_105_with_business_trip_warning():
    d = rule_doc("thsr_ticket")
    assert rules.transport_input_tax(1490) == 71 == rules.claimable_input_tax(d, CLIENT)   # 1490 x 5 / 105 = 70.95
    res = result_for(["thsr_ticket"])
    e = next(e for e in res["exceptions"] if e["rule_id"] == "TRANSPORT_TICKET")
    assert e["severity"] == "warning" and "confirm this was an employee's business trip" in e["message"].lower()
    assert "NOT_A_VOUCHER" not in ids_of(res["exceptions"], "thsr_ticket")
    row = export.rows(res)[0]
    assert (row["status"], row["format_code_格式代號"], row["deduction_code_扣抵代號"], row["tax"]) == \
        ("READY", "22", "1", 71)
    assert "business trip" in row["note"]
    recs, skipped = export.media_records(res, TAXREG)
    assert skipped == []
    rec = recs[CLIENT["ubn"]][0]
    # 第二十點(十五)1(3): tax-inclusive total, tax 0; the filer is the buyer; 其他憑證號碼 X(10)
    assert rec[0:2] == "22" and rec[23:31] == CLIENT["ubn"] and rec[31:39] == "85927754"
    assert rec[39:49] == "0512345678" and rec[49:61] == "000000001490" and rec[62:72] == "0000000000"
    assert check_record(rec, 1, CLIENT["ubn"]) == ([], [])


def test_ticket_without_carrier_ubn_goes_to_a_person():
    res = result_for(["tra_ticket_no_ubn"])
    assert {"TRANSPORT_TICKET", "READ_REVIEW"} <= ids_of(res["exceptions"], "tra_ticket_no_ubn")
    assert export.rows(res)[0]["status"] == "HOLD"


def test_ticket_naming_another_company_is_not_this_clients():
    f = dict(FIX["docs"]["bus_ticket"]["fields"], buyer_ubn="12345675")
    res = {"clients": [CLIENT], "exceptions": [], "docs": [
        {"doc_id": "b", "client_id": "A", "fields": f, "tax_voucher_kind": "transport_ticket"}]}
    assert export.rows(res)[0]["status"] == "NOT_IN_FILE"


def test_taxi_receipt_stays_non_deductible():
    res = result_for(["taxi_receipt"])
    assert "TRANSPORT_TICKET" not in ids_of(res["exceptions"], "taxi_receipt")
    assert export.rows(res)[0]["status"] == "NOT_IN_FILE"


# ---- G4: fixed assets (Audit Regulations Art. 77-1) ---------------------------------------------------------------
def _inv(doc_id, net, desc):
    return rules.Doc(doc_id, "A", "2026-10", doc_type="triplicate_invoice", invoice_no="AB12345678",
                     date=date(2026, 10, 3), seller_ubn="85927754", buyer_ubn="00501503",
                     items=[rules.Item(desc, 1, net)], net=net, tax=round(net * 0.05), total=net + round(net * 0.05))


@pytest.mark.parametrize("net,desc,hint", [
    (85000, "教育訓練課程", True),          # amount alone
    (80000, "顧問服務", True),              # at the threshold
    (30000, "筆記型電腦 14吋", True),        # equipment keyword
    (30000, "電腦維修", False),             # repair: an expense
    (500, "影印紙 A4", False),
    (79999, "顧問服務", False),
])
def test_fixed_asset_hint(net, desc, hint):
    ex = [e for e in rules.run_rules([_inv("x", net, desc)], CL) if e.rule_id == "FIXED_ASSET_HINT"]
    assert bool(ex) == hint
    if hint:
        assert ex[0].severity == "warning" and "confirm deduction code 2 (or 4 if non-deductible)" in ex[0].message
        assert ex[0].to_dict()["citations"][0]["url"].endswith("G0340051&flno=77-1")


def test_fixed_asset_hint_column_and_code_unchanged():
    res = {"clients": [CLIENT], "exceptions": [], "docs": [
        {"doc_id": "s", "client_id": "A", "fields": {"doc_type": "triplicate_invoice", "buyer_ubn": "00501503",
                                                      "date": "2026-10-03", "net": 90000, "tax": 4500, "total": 94500,
                                                      "items": [{"desc": "伺服器"}]}},
        {"doc_id": "p", "client_id": "A", "fields": {"doc_type": "triplicate_invoice", "buyer_ubn": "00501503",
                                                      "date": "2026-10-03", "net": 500, "tax": 25, "total": 525,
                                                      "items": [{"desc": "影印紙"}]}}]}
    rows = {r["doc_id"]: r for r in export.rows(res)}
    assert "fixed_asset_hint" in export.COLUMNS
    assert rows["s"]["fixed_asset_hint"].startswith("net NT$90,000") and rows["s"]["deduction_code_扣抵代號"] == "1"
    assert "may be a fixed asset" in rows["s"]["note"] and rows["p"]["fixed_asset_hint"] == ""


# ---- G8: customs-collected VAT payment certificate (format 28) ----------------------------------------------------
def test_customs_certificate_is_detected_and_held():
    res = result_for(["customs_certificate"])
    ids = ids_of(res["exceptions"], "customs_certificate")
    assert "CUSTOMS_KEY_BY_HAND" in ids and "NOT_A_VOUCHER" not in ids
    e = next(e for e in res["exceptions"] if e["rule_id"] == "CUSTOMS_KEY_BY_HAND")
    assert e["severity"] == "hold" and "繳納證號碼" in e["message"]
    row = export.rows(res)[0]
    assert (row["status"], row["format_code_格式代號"], row["tax"]) == ("HOLD", "28", 1000)
    assert export.media_records(res, TAXREG)[0] == {}         # never filed from a photo without a person


def test_customs_record_layout_when_the_fields_are_keyed(tmp_path):
    rec = export.media_record(fmt="28", tax_reg_no="123456789", serial=1, date="2026-10-09", buyer_ubn="00501503",
                              seller_ubn="", voucher_no="AA12345678901B", amount=20000, tax=1000, deduction="1")
    assert len(rec) == 81 and rec[0:2] == "28" and rec[23:31] == "00501503"
    assert rec[31:35] == "    " and rec[35:49] == "AA12345678901B"           # 32-35 blank, 36-49 certificate no.
    assert rec[49:61] == "000000020000" and rec[61] == "1" and rec[62:72] == "0000001000" and rec[72] == "1"
    assert check_record(rec, 1, "00501503") == ([], [])
    # the HOLD line, once a person keyed the fields, is written by include_hold (a dry look) with the same layout
    res = result_for(["customs_certificate"])
    paths, skipped = export.write_media_txt(res, tmp_path, TAXREG, include_hold=True)
    assert skipped == [] and check_file(paths[0])["ok"]
    assert paths[0].read_text(encoding="ascii").splitlines()[0][35:49] == "AA12345678901B"
    for bad in ("AA1234", "AA12345678901B9", ""):
        with pytest.raises(ValueError):
            export.media_record(fmt="28", tax_reg_no="123456789", serial=1, date="2026-10-09",
                                buyer_ubn="00501503", seller_ubn="", voucher_no=bad, amount=1, tax=0, deduction="1")


def test_customs_certificate_of_another_importer_claims_nothing():
    d = rule_doc("customs_certificate", client="B")
    assert rules.claimable_input_tax(d, CL["B"]) == 0 and rules.claimable_input_tax(rule_doc("customs_certificate"),
                                                                                    CLIENT) == 1000


# ---- G10: small-business plain receipts total, per client and period ----------------------------------------------
def test_period_summary_small_business_receipts(tmp_path):
    out_of_period = dict(FIX["docs"]["small_business_receipt"]["fields"], date="2026-08-20", total=900)
    res = result_for(["small_business_receipt", "taxi_receipt", "receipt_with_tax"])
    res["docs"].append({"doc_id": "old", "client_id": "A", "fields": out_of_period, "small_business_receipt": True})
    res["docs"].append({"doc_id": "s2", "client_id": "A", "small_business_receipt": True,
                        "fields": dict(FIX["docs"]["small_business_receipt"]["fields"], total=1200)})
    s = {r["client_id"]: r for r in export.period_summary(res)}
    assert s["A"]["small_business_receipts_total"] == 1500 and s["A"]["small_business_receipts_n"] == 2
    assert s["B"]["small_business_receipts_total"] == 0
    assert "401 input total" in s["A"]["note"]
    p = export.write_summary_csv(res, tmp_path / "export_summary.csv")
    got = list(csv.DictReader(p.open(encoding="utf-8-sig")))
    assert got[0]["client_id"] == "A" and got[0]["small_business_receipts_total"] == "1500"


# ---- G15 / G19 -----------------------------------------------------------------------------------------------------
def test_cross_client_duplicate_leads_with_audit_regulations_art67():
    assert rules.RULES["DUP_CROSS_CLIENT"]["citations"][0]["url"].endswith("G0340051&flno=67")
    taxi = [rules.Doc(c, c, "2026-10", doc_type="receipt", date=date(2026, 10, 12), seller_ubn="85927754",
                      items=[rules.Item("計程車資", 1, 550)], total=550) for c in ("A", "B")]

    def inv(c, buyer):
        return rules.Doc("i" + c, c, "2026-10", doc_type="triplicate_invoice", invoice_no="AB12345678",
                         date=date(2026, 10, 3), seller_ubn="85927754", buyer_ubn=buyer, net=1000, tax=50, total=1050)
    for docs in (taxi, [inv("A", "00501503"), inv("B", "12345675")]):
        ex = [e.to_dict() for e in rules.run_rules(docs, CL) if e.rule_id == "DUP_CROSS_CLIENT"]
        assert ex and all(e["citations"][0]["url"].endswith("G0340051&flno=67") for e in ex)
    vat = [e.to_dict() for e in rules.run_rules([inv("A", "00501503"), inv("B", "12345675")], CL)
           if e.rule_id == "DUP_CROSS_CLIENT" and e.doc_id == "iA"][0]
    assert any("flno=33" in c["url"] for c in vat["citations"])          # Art. 33 only where input tax is claimed
    taxi_ex = [e.to_dict() for e in rules.run_rules(taxi, CL) if e.rule_id == "DUP_CROSS_CLIENT"]
    assert not any("G0340080" in c["url"] for e in taxi_ex for c in e["citations"])


def test_ready_is_labelled_pre_checked():
    res = {"clients": [CLIENT], "exceptions": [], "docs": [
        {"doc_id": "t", "client_id": "A", "fields": {"doc_type": "triplicate_invoice", "buyer_ubn": "00501503",
                                                      "date": "2026-09-03", "net": 1000, "tax": 50, "total": 1050}}]}
    row = export.rows(res)[0]
    assert row["status"] == "READY"                          # internal id unchanged
    assert row["status_label"] == "pre-checked (a bookkeeper still signs off every line)"


# ---- platform download hook ---------------------------------------------------------------------------------------
def test_platform_invoices_default_changes_nothing():
    d = _inv("x", 1000, "影印紙")
    assert [e.rule_id for e in rules.run_rules([d], CL)] == [e.rule_id for e in rules.run_rules([d], CL,
                                                                                              platform_invoices=set())]


@pytest.mark.parametrize("plat", [{"AB12345678"}, {"A": {"AB12345678"}}])
def test_invoice_in_platform_download_is_not_entered_again(plat):
    d = _inv("x", 1000, "影印紙")
    ex = [e.to_dict() for e in rules.run_rules([d], CL, platform_invoices=plat)]
    e = next(e for e in ex if e["rule_id"] == "IN_PLATFORM")
    assert e["severity"] == "info" and e["message"].endswith("already in the platform download: not entered again.")
    res = {"clients": [CLIENT], "exceptions": ex, "docs": [
        {"doc_id": "x", "client_id": "A", "fields": {"doc_type": "triplicate_invoice", "invoice_no": "AB12345678",
                                                      "buyer_ubn": "00501503", "seller_ubn": "85927754",
                                                      "date": "2026-10-03", "net": 1000, "tax": 50, "total": 1050}}]}
    row = export.rows(res)[0]
    assert row["status"] == "NOT_IN_FILE" and "already in the platform download" in row["note"]
    assert not any(e["rule_id"] == "IN_PLATFORM" for e in
                   (x.to_dict() for x in rules.run_rules([d], CL, platform_invoices={"B": {"AB12345678"}})))


# ---- end to end through pipeline.audit (fake reader, no model) ----------------------------------------------------
class _Reader:
    model_id, device, thinking = "fake", "CPU", "off"

    def read_raw(self, path):
        return {"ocr_lines": ocr_lines(path.stem), "raw": "{}", "timing": {"total_s": 0.0}, "image_size": [10, 10]}

    def read(self, path, raw=None):
        n = path.stem
        return {"fields": json.loads(json.dumps(FIX["docs"][n]["fields"])), "provenance": {}, "boxes": {},
                "timing": {"total_s": 0.0}, "image_size": [10, 10], "ocr_lines": ocr_lines(n),
                "model": self.model_id, "device": self.device}


def test_audit_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "DATA", tmp_path / "data")
    monkeypatch.setattr(pipeline, "OUT", tmp_path / "data" / "out")
    monkeypatch.setenv("LEDGER_AUDIT_KEY_FILE", str(tmp_path / "audit.key"))
    base = tmp_path / "data" / "v"
    folder = base / "clients" / "A" / "2026-10"
    folder.mkdir(parents=True)
    (base / "clients.json").write_text(json.dumps({"clients": [CLIENT]}, ensure_ascii=False), encoding="utf-8")
    for n in ("thsr_ticket", "customs_certificate", "receipt_with_tax", "small_business_receipt", "taxi_receipt"):
        Image.new("RGB", (20, 20)).save(folder / f"{n}.jpg")
    run = pipeline.audit("v", _Reader(), run_id="vouchers-test", cache=False)
    docs = {Path(d["doc_id"]).stem: d for d in run["docs"]}
    assert docs["thsr_ticket"]["tax_voucher_kind"] == "transport_ticket"
    assert docs["customs_certificate"]["tax_voucher_kind"] == "customs_payment"
    assert docs["small_business_receipt"]["small_business_receipt"] is True
    st = {Path(r["doc_id"]).stem: (r["status"], r["format_code_格式代號"]) for r in export.rows(run)}
    assert st == {"thsr_ticket": ("READY", "22"), "customs_certificate": ("HOLD", "28"),
                  "receipt_with_tax": ("HOLD", "22"), "small_business_receipt": ("NOT_IN_FILE", None),
                  "taxi_receipt": ("NOT_IN_FILE", None)}
    assert run["summary"]["small_business_receipts"][0]["small_business_receipts_total"] == 300
    rdir = pipeline.OUT / "runs" / "vouchers-test"
    assert (rdir / "export_summary.csv").exists()
    head = (rdir / "export_media.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert "status_label" in head and "fixed_asset_hint" in head
    # the platform hook passes through audit() to the rules
    seen = {}
    real = rules.run_rules

    def spy(docs, clients, registry=None, platform_invoices=None, **kw):
        seen["p"] = platform_invoices
        return real(docs, clients, registry=registry, platform_invoices=platform_invoices, **kw)
    monkeypatch.setattr(rules, "run_rules", spy)
    pipeline.audit("v", _Reader(), run_id="vouchers-test-2", cache=False, platform_invoices={"A": {"AB12345678"}})
    assert seen["p"] == {"A": {"AB12345678"}}
