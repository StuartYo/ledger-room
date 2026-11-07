"""Code-review fixes, 2026-11-07: station / airport shops are not tickets, a ticket without a number is held,
spreadsheet formulas are neutralised, a voided platform invoice never reaches the media file, the web run wires the
platform download in."""
import csv
import inspect
from pathlib import Path

from ledger_room import einvoice_platform as plat
from ledger_room import export, filing, pipeline, rules, voucher_export

FIX = Path(__file__).parent / "fixtures" / "platform"
CLIENT = {"id": "H01", "ubn": "67094646", "name": "x", "period": ["2026-09-01", "2026-10-31"], "policy": {}}


def test_shop_in_a_station_or_airport_is_not_a_ticket():
    assert rules.other_voucher_kind("鬍鬚張 高鐵台中站店 滷肉飯 120", "receipt") is None
    assert rules.other_voucher_kind("桃園國際機場 航空站 咖啡 拿鐵 150", "receipt") is None
    assert rules.other_voucher_kind("客運站便利商店 停車票 100", "receipt") is None
    assert rules.other_voucher_kind("國光客運 台北 → 台中 票價 260", "receipt") == "transport_ticket"
    assert rules.other_voucher_kind("長榮航空 登機證", None) == "transport_ticket"


def _ticket_result(invoice_no):
    f = {"doc_type": "receipt", "seller_ubn": "85927754", "buyer_ubn": None, "invoice_no": invoice_no,
         "date": "2026-10-05", "total": 1490, "seller_name": "台灣高鐵"}
    return {"clients": [{"id": "A", "ubn": "00501503", "period": ["2026-09-01", "2026-10-31"]}], "exceptions": [],
            "docs": [{"doc_id": "t", "client_id": "A", "fields": f, "tax_voucher_kind": "transport_ticket"}]}


def test_ticket_without_a_number_is_held_not_silently_skipped():
    row = export.rows(_ticket_result(None))[0]
    assert row["status"] == "HOLD" and "ticket number not read" in row["note"]
    assert export.rows(_ticket_result("0512345678"))[0]["status"] == "READY"


def test_formula_text_is_neutralised_in_csv_and_xlsx(tmp_path):
    res = _ticket_result("0512345678")
    res["docs"][0]["fields"]["seller_name"] = '=HYPERLINK("http://x","y")'
    res["docs"][0]["fields"]["items"] = [{"desc": "+cmd|' /C calc'!A0"}]
    p = voucher_export.write(res, tmp_path / "v.csv", force_csv=True)
    row = next(csv.DictReader(p.open(encoding="utf-8-sig")))
    assert row["賣方名稱"].startswith("'=") and row["摘要"].startswith("'+")
    assert export.cell_safe(-5) == -5 and export.cell_safe("AB12345678") == "AB12345678"
    try:
        import openpyxl
    except ImportError:
        return
    x = voucher_export.write(res, tmp_path / "v.xlsx")
    ws = openpyxl.load_workbook(x).active
    cell = ws.cell(row=2, column=voucher_export.COLUMNS.index("賣方名稱") + 1)
    assert cell.data_type == "s" and cell.value.startswith("=")


def test_voided_platform_invoice_never_filed_even_with_include_hold():
    platform = plat.load_client(CLIENT, CLIENT["period"], root=FIX)
    f = {"doc_type": "e_invoice", "invoice_no": "AB00000004", "seller_ubn": "10000009", "buyer_ubn": "67094646",
         "date": "2026-10-03", "net": 1000, "tax": 50, "total": 1050}
    result = {"clients": [CLIENT], "docs": [{"doc_id": "v", "client_id": "H01", "fields": f}],
              "exceptions": [{"doc_id": "v", "client_id": "H01", "rule_id": "VOID_ON_PLATFORM", "severity": "high"}]}
    mf = filing.assemble_media_file(CLIENT, CLIENT["period"], tax_reg_no="123456789", result=result,
                                    platform=platform, include_hold=True)
    claimed = [r for r, src in zip(mf.records, mf.sources) if src == "ledger-room"]
    assert not any(r["invoice_no"] == "AB00000004" for r in claimed)
    assert any("AB00000004" in n and "voided" in n for n in mf.notes)


def test_platform_hooks_by_client_and_audit_auto():
    issued, void = plat.platform_hooks_by_client([CLIENT], root=FIX)
    assert void["H01"] == {"AB00000004"} and issued["H01"]
    assert "platform" in inspect.signature(pipeline.audit).parameters


def test_log_written_before_signing_is_unsigned_not_tampered(tmp_path):
    import json as _json
    from ledger_room.pipeline import AuditLog
    key = b"k" * 32
    p = tmp_path / "a.jsonl"
    log = AuditLog(p, key=key)
    log.add("one"); log.add("two")
    assert AuditLog.verify_detail(p, key) == {"chain_ok": True, "mac_ok": True, "n": 2}
    lines = [_json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    for e in lines:
        e.pop("mac")
    p.write_text("".join(_json.dumps(e, sort_keys=True) + "\n" for e in lines), encoding="utf-8")
    r = AuditLog.verify_detail(p, key)
    assert r["chain_ok"] and r["mac_ok"] is None and r["unsigned"] is True
    # stripping one MAC among signed lines is still a mismatch
    log2 = AuditLog(tmp_path / "b.jsonl", key=key)
    log2.add("one"); log2.add("two")
    ls = [_json.loads(x) for x in (tmp_path / "b.jsonl").read_text(encoding="utf-8").splitlines()]
    ls[1].pop("mac")
    (tmp_path / "b.jsonl").write_text("".join(_json.dumps(e, sort_keys=True) + "\n" for e in ls), encoding="utf-8")
    r2 = AuditLog.verify_detail(tmp_path / "b.jsonl", key)
    assert r2["mac_ok"] is False and "unsigned" not in r2


def test_strict_reading_needs_two_sources(monkeypatch):
    from ledger_room import pipeline, tiered
    f = {"doc_type": "e_invoice", "invoice_no": "CQ12345678", "date": "2026-10-03", "seller_ubn": "22210975",
         "total": 100}
    prov = {"doc_type": "ocr-keyword", "invoice_no": "snapped", "date": "ocr-confirmed", "seller_ubn": "ocr-role",
            "total": "ocr-confirmed"}
    u = pipeline.strict_uncertain(f, prov)
    assert set(u) == {"invoice_no"} or set(u) == {"invoice_no", "seller_ubn"}   # seller depends on its checksum
    d = {"doc_id": "A/2026-10/x.jpg", "client_id": "A", "month": "2026-10"}
    monkeypatch.setattr(pipeline, "STRICT", False)
    assert "invoice_no" not in pipeline.to_rule_doc(d, f, prov).uncertain
    monkeypatch.setattr(pipeline, "STRICT", True)
    doc = pipeline.to_rule_doc(d, f, prov)
    ex = rules.run_rules([doc], {"A": {"id": "A", "ubn": "67094646", "period": ["2026-09-01", "2026-10-31"],
                                       "policy": {}}})
    assert any(e.rule_id == "READ_REVIEW" and "invoice_no" in e.evidence_fields for e in ex)
    # the second reader agreeing with the kept OCR value confirms it
    first = {"fields": dict(f), "provenance": dict(prov), "parsed": True}
    second = {"fields": dict(f), "provenance": {**prov, "invoice_no": "ocr-confirmed"}, "parsed": True}
    assert "invoice_no_strict" in tiered.escalation_reasons(first)
    merged = tiered.merge(first, second, ["invoice_no_strict"])
    assert merged["provenance"]["invoice_no"] == "ocr-confirmed (second reader)"
    second_diff = {"fields": {**f, "invoice_no": "CO12345678"}, "provenance": {**prov, "invoice_no": "ocr-confirmed"},
                   "parsed": True}
    assert tiered.merge(first, second_diff, [])["provenance"]["invoice_no"] == "snapped"
