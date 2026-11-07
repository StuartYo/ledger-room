"""G2: accounting-software import file (one row per input voucher), synthetic scenario of tests/test_filing_scenario.py."""
import csv

import pytest

from ledger_room import einvoice_platform as plat
from ledger_room import voucher_export as ve
from tests.test_filing_scenario import CLIENT, FIX, PERIOD, result


def _by_no(rows):
    return {r["發票號碼"]: r for r in rows}


def test_columns_and_rows():
    rows = ve.rows(result())
    assert all(list(r) == ve.COLUMNS for r in rows)
    assert ve.COLUMNS == ["日期", "發票號碼", "格式代號", "賣方統編", "賣方名稱", "未稅金額", "稅額", "總額", "扣抵代號",
                          "課稅別", "摘要", "狀態", "建議科目"]
    r = _by_no(rows)
    assert set(r) == {"MB43283260", "CD22222222", "EF12345678", "AB11112222", "GQ07058325", "JC60478207"}
    d1 = r["MB43283260"]
    assert (d1["日期"], d1["格式代號"], d1["賣方統編"], d1["未稅金額"], d1["稅額"], d1["總額"], d1["扣抵代號"],
            d1["課稅別"], d1["狀態"]) == ("2026-09-14", "21", "08807719", 8000, 400, 8400, "1", "1", "pre-checked")


def test_status_and_two_part_amounts():
    r = _by_no(ve.rows(result()))
    assert r["GQ07058325"]["狀態"] == "hold" and "待處理" in r["GQ07058325"]["摘要"]
    assert (r["EF12345678"]["未稅金額"], r["EF12345678"]["稅額"], r["EF12345678"]["總額"]) == (1000, 50, 1050)
    assert r["AB11112222"]["扣抵代號"] == "3" and r["AB11112222"]["狀態"] == "pre-checked"
    # expense voucher without a buyer UBN: booked, but no format or deduction code
    assert (r["JC60478207"]["格式代號"], r["JC60478207"]["扣抵代號"]) == ("", "")


def test_account_suggestions_only_with_keyword_evidence():
    r = _by_no(ve.rows(result()))
    assert r["AB11112222"]["建議科目"] == "6121 交際費（建議）"      # 紅酒禮盒 -> entertainment keyword
    assert r["CD22222222"]["建議科目"] == "6114 旅費（建議）"        # 住宿 -> lodging
    assert r["JC60478207"]["建議科目"] == "6114 旅費（建議）"        # 計程車資 -> taxi
    assert r["MB43283260"]["建議科目"] == ""                        # 影印紙: no confident mapping


def test_in_platform_photos_are_left_out_and_platform_rows_added():
    res = result()
    res["exceptions"].append({"doc_id": "H01/2026-10/d2.jpg", "client_id": "H01", "rule_id": "IN_PLATFORM",
                              "severity": "info"})
    pin = plat.load_client(CLIENT, PERIOD, root=FIX)["input"]
    rows = ve.rows(res, pin)
    photo = [x for x in rows if x["狀態"] != "platform-unchecked"]
    assert "CD22222222" not in {x["發票號碼"] for x in photo}
    plat_rows = [x for x in rows if x["狀態"] == "platform-unchecked"]
    assert len(plat_rows) == 4
    allowance = [x for x in plat_rows if x["格式代號"] == "23"][0]
    assert (allowance["未稅金額"], allowance["稅額"], allowance["總額"]) == (-1000, -50, -1050)


def test_csv_with_bom(tmp_path):
    p = ve.write(result(), tmp_path / "import.xlsx", force_csv=True)
    assert p.suffix == ".csv"
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with p.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == ve.COLUMNS and len(rows) == 6
    assert rows[0]["賣方統編"] == "08807719"          # leading zero kept


def test_xlsx_when_openpyxl_is_installed(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = ve.write(result(), tmp_path / "import.xlsx")
    ws = openpyxl.load_workbook(p).active
    assert [c.value for c in ws[1]] == ve.COLUMNS and ws.max_row == 7
