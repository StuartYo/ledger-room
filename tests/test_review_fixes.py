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
