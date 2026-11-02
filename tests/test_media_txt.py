"""Fixed-width input records of the VAT media filing file (營業人進、銷項資料檔), 附件五 layout, 81 characters.

All UBNs, tax registration numbers and invoice numbers here are synthetic.
"""
from pathlib import Path

import pytest

from eval.media_file_check import check_file, check_record
from ledger_room import export
from ledger_room.ubn import is_valid_new

CLIENT = "67094646"          # synthetic held-out client H01
SELLER = "08807719"          # synthetic seller
TAXREG = {CLIENT: "123456789"}   # synthetic 稅籍編號 (assigned by the tax office in real life)


def _doc(doc_id, **f):
    base = {"doc_type": "triplicate_invoice", "invoice_no": "MB43283260", "date": "2026-09-14",
            "seller_ubn": SELLER, "buyer_ubn": CLIENT, "net": 4430, "tax": 222, "total": 4652}
    base.update(f)
    return {"doc_id": doc_id, "client_id": "H01", "fields": base}


def _result(docs, exceptions=()):
    return {"clients": [{"id": "H01", "ubn": CLIENT}], "docs": list(docs), "exceptions": list(exceptions)}


def test_synthetic_ubns_are_valid():
    assert is_valid_new(CLIENT) and is_valid_new(SELLER)


def test_record_positions_match_attachment_5():
    rec = export.media_record(fmt="21", tax_reg_no="123456789", serial=1, date="2026-09-14", buyer_ubn=CLIENT,
                              seller_ubn=SELLER, voucher_no="MB43283260", amount=4430, tax=222, deduction="1")
    # hand-built from 附件五, field by field
    want = ("21" + "123456789" + "0000001" + "115" + "09" + CLIENT + SELLER + "MB" + "43283260"
            + "000000004430" + "1" + "0000000222" + "1" + "     " + " " + " " + " ")
    assert rec == want
    assert len(rec) == 81
    assert rec[0:2] == "21" and rec[2:11] == "123456789" and rec[11:18] == "0000001"
    assert rec[18:21] == "115" and rec[21:23] == "09"
    assert rec[23:31] == CLIENT and rec[31:39] == SELLER and rec[39:49] == "MB43283260"
    assert rec[49:61] == "000000004430" and rec[61] == "1" and rec[62:72] == "0000000222" and rec[72] == "1"
    assert rec[73:81] == " " * 8


def test_write_media_txt_ready_only_and_validator_passes(tmp_path):
    docs = [
        _doc("H01/2026-09/a.jpg"),                                                      # READY, 21
        _doc("H01/2026-09/b.jpg", doc_type="e_invoice", invoice_no="JC60478206", net=500, tax=25, total=525),
        _doc("H01/2026-09/c.jpg", invoice_no="GQ07058325", net=1600, tax=80, total=80),   # HOLD (amount)
        _doc("H01/2026-09/d.jpg", doc_type="e_invoice", invoice_no="JC60478207", buyer_ubn=None,
             net=500, tax=None, total=500),                                              # NOT_IN_FILE
        _doc("H01/2026-09/e.jpg", invoice_no="AB11112222", items=[]),                    # READY, Art. 19 -> 3
    ]
    exc = [{"doc_id": "H01/2026-09/c.jpg", "rule_id": "AMOUNT_MISMATCH", "severity": "hold"},
           {"doc_id": "H01/2026-09/e.jpg", "rule_id": "S19_NONDEDUCTIBLE", "severity": "error"}]
    paths, skipped = export.write_media_txt(_result(docs, exc), tmp_path, TAXREG)
    assert [p.name for p in paths] == [f"{CLIENT}.TXT"]
    raw = paths[0].read_bytes()
    assert raw.endswith(b"\r\n")
    lines = raw.decode("ascii").split("\r\n")[:-1]
    assert len(lines) == 3 and all(len(x) == 81 for x in lines)
    assert [x[11:18] for x in lines] == ["0000001", "0000002", "0000003"]
    assert [x[0:2] for x in lines] == ["21", "25", "21"]
    assert [x[72] for x in lines] == ["1", "1", "3"]
    assert skipped == []
    rep = check_file(paths[0])
    assert rep["ok"], rep["errors"]
    assert rep["warnings"] == []


def test_include_hold(tmp_path):
    docs = [_doc("H01/2026-09/c.jpg", invoice_no="GQ07058325", net=1600, tax=80, total=80)]
    exc = [{"doc_id": "H01/2026-09/c.jpg", "rule_id": "AMOUNT_MISMATCH", "severity": "hold"}]
    recs, _ = export.media_records(_result(docs, exc), TAXREG)
    assert recs == {}
    recs, _ = export.media_records(_result(docs, exc), TAXREG, include_hold=True)
    assert len(recs[CLIENT]) == 1


def test_two_part_register_amount_includes_tax():
    docs = [_doc("H01/2026-09/f.jpg", doc_type="two_part_register", invoice_no="CD12345678",
                 net=None, tax=None, total=1050)]
    recs, skipped = export.media_records(_result(docs), TAXREG)
    rec = recs[CLIENT][0]
    assert rec[0:2] == "22" and rec[49:61] == "000000001050" and rec[62:72] == "0000000000"
    assert check_record(rec, 1, CLIENT) == ([], [])


def test_other_voucher_number_format_22():
    rec = export.media_record(fmt="22", tax_reg_no="123456789", serial=7, date="2026-10-01", buyer_ubn=CLIENT,
                              seller_ubn=SELLER, voucher_no="R0012345", amount=1000, tax=50, deduction="1")
    assert rec[39:49] == "R0012345  "
    assert check_record(rec, 1, CLIENT) == ([], [])


def test_missing_tax_registration_number_is_never_guessed():
    with pytest.raises(ValueError):
        export.media_records(_result([_doc("H01/2026-09/a.jpg")]), {})


def test_unusable_lines_are_reported_not_dropped():
    docs = [_doc("H01/2026-09/a.jpg", invoice_no="12345"), _doc("H01/2026-09/b.jpg", invoice_no="AB22223333")]
    recs, skipped = export.media_records(_result(docs), TAXREG)
    assert len(recs[CLIENT]) == 1
    assert [s["doc_id"] for s in skipped] == ["H01/2026-09/a.jpg"]


def _good():
    return export.media_record(fmt="21", tax_reg_no="123456789", serial=1, date="2026-09-14", buyer_ubn=CLIENT,
                               seller_ubn=SELLER, voucher_no="MB43283260", amount=4430, tax=222, deduction="1")


def _put(rec, start, end, val):
    assert len(val) == end - start + 1
    return rec[:start - 1] + val + rec[end:]


@pytest.mark.parametrize("start,end,val,field", [
    (1, 2, "19", "格式代號"),
    (3, 11, "12345678A", "申報營業人稅籍編號"),
    (22, 23, "13", "資料所屬月份"),
    (32, 39, "08807718", "銷售人統一編號"),          # bad checksum
    (24, 31, "        ", "買受人統一編號"),
    (40, 49, "M143283260", "發票字軌+號碼"),
    (50, 61, "00000000443A", "銷售金額/營業稅稅基"),
    (62, 62, "9", "課稅別"),
    (73, 73, " ", "扣抵代號"),
    (74, 78, "  X  ", "空白"),
    (80, 80, "B", "彙加註記/分攤註記"),                # B only with format 25
    (81, 81, "1", "通關方式註記"),                    # customs flag on an input record
])
def test_validator_catches_each_violation(start, end, val, field):
    errs, _ = check_record(_put(_good(), start, end, val))
    assert any(e["field"] == field for e in errs), errs


def test_validator_length_and_ascii():
    assert check_record(_good()[:80])[0][0]["field"] == "record"
    assert check_record(_good()[:80] + "臺")[0][0]["field"] == "record"


def test_validator_tax_warning_and_duplicate_serial(tmp_path):
    bad_tax = _put(_good(), 63, 72, "0000000999")
    errs, warns = check_record(bad_tax)
    assert errs == [] and warns and warns[0]["field"] == "營業稅額"
    p = tmp_path / f"{CLIENT}.TXT"
    p.write_bytes((_good() + "\r\n" + _good() + "\r\n").encode("ascii"))
    rep = check_file(p)
    assert not rep["ok"] and any(e["field"] == "流水號" for e in rep["errors"])


def test_validator_file_name(tmp_path):
    p = tmp_path / "client.txt"
    p.write_bytes((_good() + "\r\n").encode("ascii"))
    assert any(e["field"] == "file name" for e in check_file(p)["errors"])
