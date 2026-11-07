"""G3: full media file (sales + input lines) and the 401 return data file (TET_U), built from synthetic data.

Hand calculation for the scenario in tests/test_filing_scenario.py (NT$):
  Sales  35 B2B AB00000001 100,000 / 5,000; 35 B2C AB00000002 2,100 and AB00000003 1,050 tax-inclusive
         -> 3,150 split at 5/105: tax 150, amount 3,000                    box 5 = 103,000   box 6 = 5,150
         33 allowance AB00000001 2,000 / 100                                box 17 = 2,000    box 18 = 100
         31 zero-rated, customs export (flag 2) 30,000                      box 15 = 30,000
         21 = 103,000 - 2,000 = 101,000; 22 = 5,150 - 100 = 5,050; 23 = 30,000; 25 = 131,000
         AB00000004 is void: in the file, not in the totals, not counted in 使用發票份數 (4 = 31 x1 + 35 x3)
  Input  21 MB43283260 (photo) 8,000 / 400                                  box 28 / 29
         21 AB11112222 (photo, 紅酒禮盒, code 3) 3,000                       only in 48
         22 EF12345678 (photo, two-part, 1,050 incl.) -> 1,000 / 50         box 36 / 37
         25 CD11111111 (platform) 20,000 / 1,000 + CD22222222 (photo; the platform copy is dropped) 4,430 / 222
                                                                            box 32 = 24,430   box 33 = 1,222
         25 CD33333333 (platform, overridden to code 2) 90,000 / 4,500      box 34 / 35
         23 CD11111111 allowance (platform) 1,000 / 50                      box 40 / 41
         44 = 8,000 + 24,430 + 1,000 - 1,000 = 32,430   45 = 400 + 1,222 + 50 - 50 = 1,622
         46 = 90,000   47 = 4,500   48 = 32,430 + 3,000 + 500 plain receipts = 35,930   49 = 90,000
  Tax    101 = 5,050; 107 = 1,622 + 4,500 = 6,122; 110 = 6,122; 112 = 1,072; 113 = 30,000 x 5% + 4,500 = 6,000;
         114 = 1,072 (112 < 113); 115 = 0; 111 = 0
"""
import shutil

import pytest

from eval.filing_check import check_pair, check_tet_u, decode_signed
from eval.media_file_check import check_file
from ledger_room import filing
from ledger_room import einvoice_platform as plat
from tests.test_filing_scenario import CLIENT, FIX, GOLDEN, HEADER, PERIOD, TAXREG, UBN, rec, result

WANT = {1: 0, 2: 0, 5: 103000, 6: 5150, 7: 0, 15: 30000, 17: 2000, 18: 100, 19: 0, 21: 101000, 22: 5050,
        23: 30000, 24: 0, 25: 131000, 28: 8000, 29: 400, 30: 0, 31: 0, 32: 24430, 33: 1222, 34: 90000, 35: 4500,
        36: 1000, 37: 50, 38: 0, 39: 0, 40: 1000, 41: 50, 42: 0, 43: 0, 44: 32430, 45: 1622, 46: 90000, 47: 4500,
        48: 35930, 49: 90000, 78: 0, 79: 0, 80: 0, 81: 0, 101: 5050, 103: 0, 104: 0, 105: 0, 106: 5050,
        107: 6122, 108: 0, 109: 0, 110: 6122, 111: 0, 112: 1072, 113: 6000, 114: 1072, 115: 0}


@pytest.fixture
def filed(tmp_path):
    p = plat.load_client(CLIENT, PERIOD, root=FIX)
    return filing.file_return(CLIENT, PERIOD, tmp_path, tax_reg_no=TAXREG, header=HEADER, result=result(),
                              platform=p, deduction_overrides={"CD33333333": "2"}, plain_receipts=500)


def test_box_totals_match_the_hand_calculation(filed):
    b = filed["totals"]["boxes"]
    assert {k: b[k] for k in WANT} == WANT
    assert filed["totals"]["invoices_used"] == 4


def test_golden_files(filed):
    assert filed["txt"].read_bytes() == (GOLDEN / f"{UBN}.TXT").read_bytes()
    assert filed["tet_u"].read_bytes() == (GOLDEN / f"{UBN}.TET_U").read_bytes()


def test_media_file_contents(filed):
    mf = filed["media"]
    assert len(mf.lines) == 13
    assert [x[11:18] for x in mf.lines] == [f"{i:07d}" for i in range(1, 14)]
    assert all(x[2:11] == TAXREG for x in mf.lines)
    assert mf.sources.count("platform-sales") == 6 and mf.sources.count("ledger-room") == 4
    assert mf.sources.count("platform-input") == 3
    assert mf.duplicates == [{"voucher": "CD22222222", "kept": "ledger-room", "dropped": "platform-input",
                              "differs": {}}]
    # hand-built: the two-part photo line (format 22, tax-inclusive amount, tax 0)
    assert ("22" + TAXREG + "0000003" + "11510" + UBN + "10000015" + "EF12345678" + "000000001050" + "1"
            + "0000000000" + "1" + " " * 8) in mf.lines
    # the platform's computer, deduction code overridden to 2
    assert [x[72] for x in mf.lines if x[39:49] == "CD33333333"] == ["2"]
    # void sales invoice kept in the file with 課稅別 F
    assert [x[61] for x in mf.lines if x[39:49] == "AB00000004"] == ["F"]
    # HOLD (d5) and expense-only (d6) photos are not in the file
    assert not any(x[39:49] in ("GQ07058325", "JC60478207") for x in mf.lines)


def test_independent_checkers_pass(filed):
    assert check_file(filed["txt"])["ok"]
    rep = check_pair(filed["txt"], filed["tet_u"])
    assert rep["ok"], rep["errors"]


def test_tet_u_fields(filed):
    raw = filed["tet_u"].read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and raw.endswith(b"\r\n") and raw.count(b"\r\n") == 1
    f = raw.decode("utf-8").rstrip("\r\n").split("|")
    assert len(f) == 112
    assert f[:8] == ["1", "00000000", UBN, "11510", "1", TAXREG, "0", "0000000004"]
    assert f[9] == "00000010300{"          # field 10 = 代號 5: 103,000
    assert f[15] == "000000515{"            # field 16 = 代號 6: 5,150
    assert f[86] == "000000612B"            # field 87 = 代號 107: 6,122
    assert f[71] == "000"                   # field 72 = 不得扣抵比例: blank on a 401
    assert f[95:104] == ["1", "D", "2", "", "王小明", "06", "2000000", "", "台財北國稅字第0000000000X號"]


def test_signed_encoding():
    assert filing.signed(0, 10) == "000000000{"
    assert filing.signed(5000, 12) == "00000000500{"
    assert filing.signed(-300, 10) == "000000030}"
    assert filing.signed(-1234, 10) == "000000123M"
    for v in (0, 7, -7, 123456, -98765):
        assert decode_signed(filing.signed(v, 10)) == v
    with pytest.raises(ValueError):
        filing.signed(10 ** 12, 12)


def test_checker_catches_a_wrong_box(filed, tmp_path):
    bad = tmp_path / "bad" / f"{UBN}.TET_U"
    bad.parent.mkdir()
    f = filed["tet_u"].read_text(encoding="utf-8").rstrip("\r\n").split("|")
    f[86] = filing.signed(6000, 10)         # 107 no longer 45 + 47
    bad.write_bytes(("|".join(f) + "\r\n").encode("utf-8"))
    errs = check_tet_u(bad)["errors"]
    assert any("代號 107" in e for e in errs)
    bad.write_bytes(b"\xef\xbb\xbf" + ("|".join(f) + "\r\n").encode("utf-8"))
    assert any("BOM" in e for e in check_tet_u(bad)["errors"])


def test_checker_cross_checks_txt_against_tet_u(filed, tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    shutil.copy(filed["tet_u"], d / f"{UBN}.TET_U")
    lines = filed["txt"].read_bytes().decode("ascii").split("\r\n")[:-1]
    lines = [x for x in lines if x[39:49] != "MB43283260"]           # drop one input line, keep the return
    lines = [x[:11] + f"{i:07d}" + x[18:] for i, x in enumerate(lines, 1)]
    (d / f"{UBN}.TXT").write_bytes("".join(x + "\r\n" for x in lines).encode("ascii"))
    rep = check_pair(d / f"{UBN}.TXT", d / f"{UBN}.TET_U")
    assert any("代號 28" in e for e in rep["errors"])


def test_platform_tax_reg_no_is_replaced_and_noted(tmp_path):
    p = plat.load_client(CLIENT, PERIOD, root=FIX)
    mf = filing.assemble_media_file(CLIENT, PERIOD, tax_reg_no="987654321", platform=p)
    assert all(x[2:11] == "987654321" for x in mf.lines)
    assert any("replaced" in n for n in mf.notes)
    with pytest.raises(ValueError):
        filing.assemble_media_file(CLIENT, PERIOD, tax_reg_no="12345", platform=p)


def test_sales_import_lines_are_added_once():
    p = plat.load_client(CLIENT, PERIOD, root=FIX)
    extra = rec("35", "11510", "", UBN, "AB00000009", 2100, "1", 0)
    mf = filing.assemble_media_file(CLIENT, PERIOD, tax_reg_no=TAXREG, platform=p, sales_lines=[extra, extra])
    assert mf.sources.count("sales-import") == 1
    with pytest.raises(ValueError):          # an input line is not a sales line
        filing.assemble_media_file(CLIENT, PERIOD, tax_reg_no=TAXREG, platform=p,
                                   sales_lines=[rec("25", "11510", UBN, "08807719", "CD99999999", 100, "1", 5, "1")])


def test_client_form_flag():
    assert filing.client_form({"ubn": UBN}) == "401"
    assert filing.client_form({"ubn": UBN, "form": "403"}) == "403"
    with pytest.raises(filing.NotSupported):
        filing.client_form({"ubn": UBN, "form": "404"})
    with pytest.raises(ValueError):
        filing.client_form({"ubn": UBN, "form": "405"})


def _recs(*lines):
    return [plat.parse_record(x) for x in lines]


def test_401_refuses_exempt_sales_and_special_rates():
    exempt = _recs(rec("31", "11510", "10000004", UBN, "AB00000001", 1000, "3", 0))
    with pytest.raises(ValueError, match="403"):
        filing.return_totals(exempt, form="401")
    special = _recs(rec("37", "11510", "", UBN, "AB00000001", 1000, "1", 0, special="1"))
    with pytest.raises(filing.NotSupported):
        filing.return_totals(special, form="403")
    with pytest.raises(filing.NotSupported):
        filing.return_totals([], form="404")
    no_flag = _recs(rec("31", "11510", "", UBN, "AB00000001", 1000, "2", 0))
    with pytest.raises(ValueError, match="通關方式註記"):
        filing.return_totals(no_flag, form="401")


def test_customs_and_two_part_split():
    """28 customs certificate goes to 78 / 79 at its own tax; a 24 allowance on a two-part invoice is split at 5/105."""
    customs = "28" + TAXREG + "0000001" + "11510" + UBN + "    " + "BC1234567890AB" + "000000020000" + "1" \
              + "0000001000" + "1" + " " * 8
    rs = _recs(customs, rec("24", "11510", UBN, "10000015", "EF12345678", 210, "1", 0, "1"))
    b = filing.return_totals(rs, form="401")["boxes"]
    assert (b[78], b[79]) == (20000, 1000)
    assert (b[40], b[41]) == (200, 10)
    assert (b[44], b[45]) == (20000 - 200, 1000 - 10)
