"""G9: form 403 (兼營免稅): 不得扣抵比例 and the deductible input tax, with hand-computed numbers.

Rules: 403 填寫說明(十三)(十四) (ratio = (④+⑤-⑧)/(⑦-⑧), fraction of a percent dropped; 51 = (⑨+⑩) x (1-⑪));
兼營營業人營業稅額計算辦法 Art. 3, 4 (per period) and Art. 7 (year-end adjustment). Synthetic numbers only.
"""
import pytest

from eval.filing_check import check_tet_u
from ledger_room import filing
from ledger_room import einvoice_platform as plat
from tests.test_filing_scenario import HEADER, TAXREG, UBN, rec


def _recs(*lines):
    return [plat.parse_record(x) for x in lines]


TAXABLE = rec("35", "11510", "10000004", UBN, "AB00000001", 600000, "1", 30000)
EXEMPT = rec("31", "11510", "10000009", UBN, "AB00000002", 400000, "3", 0)
# 50317242: a made-up seller UBN with a valid check digit, unregistered in the FIA registration API (checked 2026-11-09)
IN_GOODS = rec("25", "11510", UBN, "50317242", "CD11111111", 200000, "1", 10000, "1")
IN_FIXED = rec("25", "11510", UBN, "10000010", "CD22222222", 100000, "1", 5000, "2")
IN_S19 = rec("21", "11510", UBN, "10000021", "CD33333333", 20000, "1", 1000, "3")


def test_ratio_and_deductible_tax():
    """Exempt 400,000 of 1,000,000 -> 40 %. Deductible input tax 10,000 + 5,000 = 15,000 x 60 % = 9,000.
    The code-3 line (Art. 19) is outside 45 / 47 before the ratio is applied (辦法 Art. 4)."""
    t = filing.return_totals(_recs(TAXABLE, EXEMPT, IN_GOODS, IN_FIXED, IN_S19), form="403",
                             period=("2026-09-01", "2026-10-31"))
    b = t["boxes"]
    assert (b[21], b[22], b[24], b[25]) == (600000, 30000, 400000, 1000000)
    assert (b[45], b[47]) == (10000, 5000)
    assert t["nondeductible_ratio_pct"] == 40 and b[50] == 40
    assert b[51] == 9000 and b[107] == 9000
    assert (b[101], b[106], b[110], b[111], b[112]) == (30000, 30000, 9000, 21000, 0)
    assert b[48] == 200000 + 20000 and b[49] == 100000
    assert t["notes"] == []


def test_fraction_of_a_percent_is_dropped():
    """Exempt 100,000 / (200,000 + 100,000) = 33.33 % -> 33. 51 = 3,000 x 67 % = 2,010."""
    rs = _recs(rec("35", "11510", "10000004", UBN, "AB00000001", 200000, "1", 10000),
               rec("31", "11510", "10000009", UBN, "AB00000002", 100000, "3", 0),
               rec("25", "11510", UBN, "50317242", "CD11111111", 60000, "1", 3000, "1"))
    b = filing.return_totals(rs, form="403")["boxes"]
    assert b[50] == 33 and b[51] == 2010


def test_land_sales_are_left_out_of_the_ratio():
    """Exempt 400,000 includes land 100,000: (400,000 - 100,000) / (1,000,000 - 100,000) = 33.33 % -> 33."""
    t = filing.return_totals(_recs(TAXABLE, EXEMPT, IN_GOODS), form="403", land_sales=100000)
    assert t["boxes"][50] == 33 and t["boxes"][26] == 100000
    assert t["boxes"][51] == 6700            # 10,000 x 67 %


def test_negative_taxable_sales_count_as_zero():
    """①+③ < 0 counts as 0 (填寫說明(十三)): a period with only a taxable return and exempt sales -> 100 %."""
    rs = _recs(rec("33", "11510", "10000004", UBN, "AB00000001", 5000, "1", 250),
               rec("31", "11510", "10000009", UBN, "AB00000002", 50000, "3", 0),
               rec("25", "11510", UBN, "50317242", "CD11111111", 2000, "1", 100, "1"))
    b = filing.return_totals(rs, form="403")["boxes"]
    assert b[21] == -5000 and b[50] == 100 and b[51] == 0


def test_no_sales_gives_ratio_zero_with_a_note():
    t = filing.return_totals(_recs(IN_GOODS), form="403")
    assert t["boxes"][50] == 0 and t["boxes"][51] == 10000 and t["notes"]


def test_ratio_function_direct():
    assert filing.nondeductible_ratio(600000, 400000) == (40, None)
    assert filing.nondeductible_ratio(2, 1)[0] == 33
    assert filing.nondeductible_ratio(0, 0)[0] == 0


def test_year_end_adjustment():
    """辦法 Art. 7: year input tax 100,000, Art. 19 non-deductible 4,000, already deducted 60,000, year ratio 35 %:
    allowed = 96,000 x 65 % = 62,400; adjustment = 60,000 - 62,400 = -2,400 -> refund, box 109."""
    adj = filing.annual_adjustment(100000, 4000, 60000, 35)
    assert adj == -2400
    t = filing.return_totals(_recs(TAXABLE, EXEMPT, IN_GOODS), form="403", period=("2026-11-01", "2026-12-31"),
                             year_end_adjustment=adj)
    b = t["boxes"]
    assert (b[105], b[109]) == (0, 2400)
    assert b[110] == b[107] + b[108] + 2400
    assert filing.annual_adjustment(100000, 0, 70000, 40) == 10000      # 70,000 - 60,000: tax to pay
    t2 = filing.return_totals(_recs(TAXABLE, EXEMPT, IN_GOODS), form="403", year_end_adjustment=10000)
    assert t2["boxes"][105] == 10000 and t2["boxes"][106] == 30000 + 10000


def test_last_period_without_adjustment_gets_a_note():
    t = filing.return_totals(_recs(TAXABLE, EXEMPT, IN_GOODS), form="403", period=("2026-11-01", "2026-12-31"))
    assert any("Art. 7" in n for n in t["notes"])


def test_403_tet_u(tmp_path):
    t = filing.return_totals(_recs(TAXABLE, EXEMPT, IN_GOODS, IN_FIXED), form="403")
    line = filing.tet_u_line(t, ubn=UBN, tax_reg_no=TAXREG, period_end="2026-10", **HEADER)
    f = line.split("|")
    assert f[0] == "3" and f[71] == "040" and f[72] == filing.signed(9000, 10)
    p = filing.write_tet_u(line, tmp_path, UBN)
    rep = check_tet_u(p)
    assert rep["ok"], rep["errors"]
    assert rep["boxes"][51] == 9000 and rep["boxes"][50] == 40


def test_403_client_end_to_end(tmp_path):
    client = {"id": "X", "ubn": UBN, "form": "403", "period": ["2026-09-01", "2026-10-31"]}
    pdata = {"client_ubn": UBN, "months": None, "files": [], "input": _recs(IN_GOODS, IN_FIXED),
             "output": _recs(TAXABLE, EXEMPT), "void": [], "unused": [], "other_ubn": [], "errors": []}
    out = filing.file_return(client, ("2026-09-01", "2026-10-31"), tmp_path, tax_reg_no=TAXREG, header=HEADER,
                             platform=pdata)
    assert out["summary"]["form"] == "403" and out["summary"]["nondeductible_ratio_pct"] == 40
    from eval.filing_check import check_pair
    rep = check_pair(out["txt"], out["tet_u"])
    assert rep["ok"], rep["errors"]


def test_404_is_not_supported(tmp_path):
    with pytest.raises(filing.NotSupported):
        filing.file_return({"ubn": UBN, "form": "404"}, "2026-10", tmp_path, tax_reg_no=TAXREG, header=HEADER,
                           platform={"client_ubn": UBN, "months": None, "files": [], "input": [], "output": [],
                                     "void": [], "unused": [], "other_ubn": [], "errors": []})
