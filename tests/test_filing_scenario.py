"""Synthetic filing scenario shared by tests/test_filing_*.py and tests/test_voucher_export*.py.

Client H01 (UBN 67094646, synthetic), period 2026-09/2026-10 (ROC 115 09-10). Photographed vouchers (Ledger Room
result) plus the synthetic platform download in tests/fixtures/platform/67094646/. All numbers are made up.
"""
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "platform"
GOLDEN = FIX / "golden"
UBN = "67094646"
TAXREG = "123456789"
PERIOD = ("2026-09-01", "2026-10-31")
CLIENT = {"id": "H01", "ubn": UBN, "name": "白鷺旅行社有限公司", "period": list(PERIOD), "form": "401"}
HEADER = {"county": "D", "self_or_agent": "2", "agent_reg_no": "台財北國稅字第0000000000X號", "filer_name": "王小明",
          "phone_area": "06", "phone": "2000000"}


def _doc(doc_id, **f):
    base = {"doc_type": "triplicate_invoice", "seller_ubn": "08807719", "buyer_ubn": UBN, "items": []}
    base.update(f)
    return {"doc_id": f"H01/{doc_id}", "client_id": "H01", "fields": base}


def result():
    docs = [
        _doc("2026-09/d1.jpg", invoice_no="MB43283260", date="2026-09-14", net=8000, tax=400, total=8400,
             seller_name="大同文具行", items=[{"desc": "影印紙"}]),
        _doc("2026-10/d2.jpg", doc_type="e_invoice", invoice_no="CD22222222", date="2026-10-03", seller_ubn="10000009",
             net=4430, tax=222, total=4652, seller_name="示範商旅", items=[{"desc": "住宿"}]),
        _doc("2026-10/d3.jpg", doc_type="two_part_register", invoice_no="EF12345678", date="2026-10-05",
             seller_ubn="10000015", net=None, tax=None, total=1050, seller_name="示範超商", items=[{"desc": "文具"}]),
        _doc("2026-10/d4.jpg", invoice_no="AB11112222", date="2026-10-08", seller_ubn="10000021", net=3000, tax=150,
             total=3150, seller_name="示範酒莊", items=[{"desc": "紅酒禮盒"}]),
        _doc("2026-10/d5.jpg", invoice_no="GQ07058325", date="2026-10-09", net=1600, tax=80, total=80),
        _doc("2026-10/d6.jpg", doc_type="e_invoice", invoice_no="JC60478207", date="2026-10-10", buyer_ubn=None,
             net=None, tax=None, total=500, seller_name="計程車", items=[{"desc": "計程車資"}]),
    ]
    exc = [
        {"doc_id": "H01/2026-10/d4.jpg", "client_id": "H01", "rule_id": "S19_NONDEDUCTIBLE", "severity": "medium"},
        {"doc_id": "H01/2026-10/d5.jpg", "client_id": "H01", "rule_id": "AMOUNT_MISMATCH", "severity": "hold"},
    ]
    return {"clients": [{"id": "H01", "ubn": UBN, "name": CLIENT["name"]}], "docs": docs, "exceptions": exc}


def rec(fmt, ym, buyer, seller, no, amount, tt, tax, ded=" ", customs=" ", serial=1, special=" "):
    """Hand-built 81-character record (附件五), for small return-total cases."""
    r = (fmt + TAXREG + f"{serial:07d}" + ym + buyer.ljust(8) + seller.ljust(8) + no.ljust(10)
         + f"{amount:012d}" + tt + f"{tax:010d}" + ded + " " * 5 + special + " " + customs)
    assert len(r) == 81
    return r
