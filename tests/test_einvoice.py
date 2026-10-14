"""E-invoice platform import on a synthetic fixture (tests/fixtures; every value is made up)."""
from pathlib import Path

from ledger_room import einvoice, rules

FIX = Path(__file__).parent / "fixtures" / "einvoice_platform_sample.csv"


def test_groups_lines_and_skips_footnotes():
    invs, st = einvoice.read_platform_csv(FIX)
    assert [i.invoice_no for i in invs] == ["AB10000001", "AB10000002", "AB10000003", "AB10000004"]
    assert st["skipped_rows"] == 2 and st["line_items"] == 6
    two = invs[1]
    assert len(two.lines) == 3 and two.discount_lines == 1 and two.zero_lines == 1
    assert two.amount == 70 and two.amount_basis == "per-line"
    assert invs[2].voided


def test_big5_export_decodes(tmp_path):
    p = tmp_path / "big5.csv"
    p.write_bytes(FIX.read_text(encoding="utf-8-sig").encode("cp950"))
    invs, _ = einvoice.read_platform_csv(p)
    assert len(invs) == 4 and invs[0].seller_name == "示範便利商店"


def test_invoice_level_amount_column():
    txt = FIX.read_text(encoding="utf-8-sig").replace(",-10,開立", ",80,開立").replace("AB10000002,0,", "AB10000002,80,")
    invs, _ = einvoice.parse(txt)
    assert invs[1].amount == 80 and invs[1].amount_basis == "invoice"


def test_rules_run_on_imported_invoices():
    invs, _ = einvoice.read_platform_csv(FIX)
    live = [i for i in invs if not i.voided]
    client = {"id": "P", "name": "示範公司", "ubn": "00501503", "period": ["2026-09-01", "2026-10-31"], "policy": {}}
    ex = rules.run_rules([einvoice.to_rule_doc(i, "P") for i in live], {"P": client})
    assert not [e for e in ex if e.rule_id == "UBN_CHECKSUM"]          # both seller numbers are valid
    asif = rules.run_rules([einvoice.to_rule_doc(i, "P", buyer_ubn="00501503") for i in live], {"P": client})
    s19 = {e.doc_id for e in asif if e.rule_id == "S19_NONDEDUCTIBLE"}
    assert s19 == {"P/2026-09/AB10000002"}                             # the beer invoice


def test_duplicate_seen_across_exports():
    invs, _ = einvoice.read_platform_csv(FIX)
    a = einvoice.to_rule_doc(invs[0], "P")
    b = einvoice.to_rule_doc(invs[0], "Q")
    clients = {k: {"id": k, "name": k, "ubn": "00501503", "period": ["2026-09-01", "2026-10-31"], "policy": {}}
               for k in ("P", "Q")}
    ids = {e.rule_id for e in rules.run_rules([a, b], clients)}
    assert "DUP_CROSS_CLIENT" in ids
