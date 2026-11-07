"""G1: reading the e-invoice platform's media filing download (81-character records, 附件五 layout).

The fixture tests/fixtures/platform/67094646/115-09_10/67094646.txt is synthetic (all UBNs, numbers and amounts made
up) and passes eval/media_file_check.py; it imitates the platform file shown on p. 167 of the platform manual.
"""
from pathlib import Path

import pytest

from eval.media_file_check import check_file
from ledger_room import einvoice_platform as plat
from ledger_room import rules

FIX = Path(__file__).parent / "fixtures" / "platform"
CLIENT = {"id": "H01", "ubn": "67094646", "period": ["2026-09-01", "2026-10-31"]}
PERIOD = ("2026-09-01", "2026-10-31")


def test_fixture_passes_the_independent_layout_check():
    rep = check_file(FIX / "67094646" / "115-09_10" / "67094646.txt")
    assert rep["ok"], rep["errors"]


def test_parse_sales_record_positions():
    rec = "35123456789000000111509" + "10000004" + "67094646" + "AB00000001" + "000000100000" + "1" + "0000005000" \
          + " " * 9
    r = plat.parse_record(rec)
    assert (r["side"], r["format_code"], r["tax_reg_no"], r["serial"]) == ("output", "35", "123456789", "0000001")
    assert (r["month"], r["year_roc"]) == ("2026-09", 115)
    assert (r["buyer_ubn"], r["seller_ubn"], r["invoice_no"]) == ("10000004", "67094646", "AB00000001")
    assert (r["amount"], r["tax"], r["tax_type"], r["deduction"]) == (100000, 5000, "1", None)
    assert r["status"] == "issued" and not r["tax_inclusive"]


def test_parse_rejects_bad_lines():
    with pytest.raises(ValueError):
        plat.parse_record("35" + "1" * 70)
    with pytest.raises(ValueError):
        plat.parse_record("99" + "0" * 79)
    recs, errs = plat.parse_media_text("garbage line\r\n" + "35123456789000000111509" + " " * 8 + "67094646"
                                       + "AB00000001" + "0" * 12 + "F" + "0" * 10 + " " * 9 + "\r\n")
    assert len(recs) == 1 and recs[0]["status"] == "void"
    assert errs and errs[0]["line"] == 1          # reported, not dropped quietly


def test_load_client_filters_period_client_and_status():
    d = plat.load_client(CLIENT, PERIOD, root=FIX)
    assert d["errors"] == []
    assert [r["invoice_no"] for r in d["output"]] == ["AB00000001", "AB00000002", "AB00000003", "AB00000001",
                                                      "AB00000005"]
    assert [r["invoice_no"] for r in d["void"]] == ["AB00000004"]
    assert [r["invoice_no"] for r in d["input"]] == ["CD11111111", "CD22222222", "CD11111111", "CD33333333"]
    assert [r["invoice_no"] for r in d["other_ubn"]] == ["CD44444444"]     # another business's line
    allowances = [r for r in d["input"] + d["output"] if r["is_allowance"]]
    assert [(r["format_code"], r["status"]) for r in allowances] == [("23", "allowance"), ("33", "allowance")]
    b2c = [r for r in d["output"] if r["buyer_ubn"] is None and r["tax_type"] == "1"]
    assert all(r["tax_inclusive"] for r in b2c)


def test_platform_invoices_exclude_void_keep_allowances():
    inv = plat.platform_invoices(CLIENT, PERIOD, root=FIX)
    assert all(r["status"] != "void" for r in inv)
    assert {"23", "33"} <= {r["format_code"] for r in inv}


def test_platform_invoice_numbers_for_the_pipeline_hook():
    nums = plat.platform_invoice_numbers(CLIENT, PERIOD, root=FIX)
    assert nums == {"CD11111111", "CD22222222", "CD33333333"}
    # whole file (period None) also catches an August invoice photographed late
    assert "CD55555555" in plat.platform_invoice_numbers(CLIENT, None, root=FIX)
    assert plat.platform_void_numbers(CLIENT, PERIOD, root=FIX) == {"AB00000004"}
    assert plat.platform_numbers_by_client([CLIENT], root=FIX) == {"H01": nums}


def test_hook_marks_photographed_invoice_in_platform():
    """The set plugs into rules.run_rules(platform_invoices=...) (another helper's hook)."""
    import inspect
    if "platform_invoices" not in inspect.signature(rules.run_rules).parameters:
        pytest.skip("rules.run_rules has no platform_invoices hook in this checkout")
    d = rules.Doc(doc_id="H01/2026-10/x.jpg", client_id="H01", month="2026-10", doc_type="e_invoice",
                  invoice_no="CD22222222", seller_ubn="10000009", buyer_ubn="67094646", net=4430, tax=222, total=4652)
    from datetime import date
    d.date = date(2026, 10, 3)
    clients = {"H01": {"id": "H01", "ubn": "67094646", "name": "x", "period": list(PERIOD), "policy": {}}}
    ex = rules.run_rules([d], clients, platform_invoices={"H01": plat.platform_invoice_numbers(CLIENT, PERIOD,
                                                                                               root=FIX)})
    assert "IN_PLATFORM" in {e.rule_id for e in ex}


def test_voided_on_platform_is_held():
    """A photographed invoice the platform marks 作廢 gets VOID_ON_PLATFORM and is not claimed (Art. 51(1)(5))."""
    from datetime import date
    d = rules.Doc(doc_id="H01/2026-10/v.jpg", client_id="H01", month="2026-10", doc_type="e_invoice",
                  invoice_no="AB00000004", seller_ubn="10000009", buyer_ubn="67094646", net=1000, tax=50, total=1050)
    d.date = date(2026, 10, 3)
    clients = {"H01": {"id": "H01", "ubn": "67094646", "name": "x", "period": list(PERIOD), "policy": {}}}
    void = {"H01": plat.platform_void_numbers(CLIENT, PERIOD, root=FIX)}
    ex = rules.run_rules([d], clients, platform_void=void)
    hit = [e for e in ex if e.rule_id == "VOID_ON_PLATFORM"]
    assert hit and hit[0].severity == "high"
    assert "VOID_ON_PLATFORM" not in {e.rule_id for e in rules.run_rules([d], clients)}
    from ledger_room import export
    assert "VOID_ON_PLATFORM" in export.BLOCKING and "VOID_ON_PLATFORM" in rules.RULES


def test_repeated_downloads_are_deduplicated(tmp_path):
    src = (FIX / "67094646" / "115-09_10" / "67094646.txt").read_bytes()
    for sub in ("a", "b"):
        p = tmp_path / "67094646" / sub
        p.mkdir(parents=True)
        (p / "67094646.txt").write_bytes(src)
    d = plat.load_client(CLIENT, PERIOD, root=tmp_path)
    assert len(d["input"]) == 4 and len(d["output"]) == 5


def test_period_months():
    assert plat.period_months(("2026-11-01", "2026-12-31")) == ["2026-11", "2026-12"]
    assert plat.period_months("2026-12_2027-01") == ["2026-12", "2027-01"]
    assert plat.period_months("2026-09") == ["2026-09"]
    assert plat.period_months(None) is None
