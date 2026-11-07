"""Period check (ledger_room/period_check.py) on a small hand-built firm, separate from the measured synthetic set.

Every UBN, invoice number and amount here is made up."""
import json
from pathlib import Path

import pytest

from eval import period_check as ev
from ledger_room import period_check as pc
from ledger_room.export import media_record

A_UBN, B_UBN = "00501503", "12345675"                 # valid test UBNs (python-stdnum examples)
S1, S2 = "53212539", "04595257"                       # sellers
PERIOD = ["2026-09-01", "2026-10-31"]
CLIENTS = {"A": {"id": "A", "name": "甲（虛構）", "ubn": A_UBN, "period": PERIOD, "policy": {}},
           "B": {"id": "B", "name": "乙（虛構）", "ubn": B_UBN, "period": PERIOD, "policy": {}}}


def rec(buyer, seller, no, date, net, fmt="25", tax_type="1", serial=1):
    tax = 0 if tax_type == "F" else round(net * 0.05)
    return media_record(fmt=fmt, tax_reg_no="123456789", serial=serial, date=date, buyer_ubn=buyer, seller_ubn=seller,
                        voucher_no=no, amount=0 if tax_type == "F" else net, tax=tax, deduction="1",
                        tax_type=tax_type)


def write(p: Path, lines):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(("\r\n".join(lines) + "\r\n").encode("ascii"))


def paper_doc(cid, n, no, seller, date, net, buyer="client", doc_type="triplicate_invoice", total=None):
    b = CLIENTS[cid]["ubn"] if buyer == "client" else buyer
    tax = round(net * 0.05) if b else None
    return {"doc_id": f"{cid}/{date[:7]}/x{n}.jpg", "client_id": cid, "month": date[:7],
            "fields": {"doc_type": doc_type, "invoice_no": no, "date": date, "seller_name": "賣方（虛構）",
                       "seller_ubn": seller, "buyer_ubn": b, "buyer_name": None,
                       "items": [{"desc": "影印紙", "qty": 1, "amount": net}], "net": net if b else None, "tax": tax,
                       "total": total if total is not None else net + (tax or 0)}}


@pytest.fixture()
def firm(tmp_path):
    pr, fr = tmp_path / "platform", tmp_path / "filed"
    write(pr / A_UBN / "p" / f"{A_UBN}.txt", [
        rec(A_UBN, S1, "AA00000001", "2026-09-03", 1000),           # clean
        rec(A_UBN, S1, "AA00000002", "2026-09-04", 2000),           # in last period's filing -> DUP_PRIOR_FILING
        rec(A_UBN, S2, "AA00000003", "2026-09-05", 3000),           # same number filed a year ago, other seller: ok
        rec(A_UBN, S1, "AA00000004", "2026-10-01", 4000),           # photo with another total -> PLATFORM_AMOUNT_DIFF
        rec(A_UBN, S1, "AA00000005", "2026-10-02", 5000),           # photo with the same total: IN_PLATFORM only
        rec(A_UBN, S2, "AA00000006", "2026-10-03", 6000),           # carrier item 禮盒 -> S19
        rec(A_UBN, S2, "AA00000007", "2026-10-04", 90000),          # FIXED_ASSET_HINT
        rec(A_UBN, S1, "AA00000008", "2026-10-05", 800),            # photographed in B's box -> DUP_CROSS_CLIENT (B)
        rec(A_UBN, S1, "AA00000009", "2026-10-06", 900),            # twice, different amounts -> conflict
        rec(A_UBN, S1, "AA00000010", "2026-10-07", 0, tax_type="F"),   # voided: left out
    ])
    write(pr / A_UBN / "q" / f"{A_UBN}.txt", [rec(A_UBN, S1, "AA00000009", "2026-10-06", 990),
                                              rec(A_UBN, S1, "AA00000001", "2026-09-03", 1000)])   # identical: merged
    (pr / A_UBN / "carrier.csv").write_text(
        "發票日期,發票號碼,發票金額,發票狀態,折讓,賣方統一編號,賣方名稱,買方統編,消費明細_數量,消費明細_單價,消費明細_金額,消費明細_品名\n"
        f"20261003,AA00000006,6300,已確認,否,{S2},x,{A_UBN},1,6300,6300,精緻禮盒\n"
        f"20261003,AA00000001,1050,已確認,否,{S1},x,{A_UBN},1,1050,1050,影印紙\n", encoding="utf-8")
    write(pr / B_UBN / "p" / f"{B_UBN}.txt", [rec(B_UBN, S2, "BB00000001", "2026-09-10", 700)])
    write(fr / A_UBN / "115-07_08" / f"{A_UBN}.TXT", [rec(A_UBN, S1, "AA00000002", "2026-08-30", 2000)])
    write(fr / A_UBN / "114-09_10" / f"{A_UBN}.TXT", [rec(A_UBN, S1, "AA00000003", "2025-09-01", 3000)])
    write(fr / B_UBN / "115-07_08" / f"{B_UBN}.TXT", [rec(B_UBN, S2, "BB00000099", "2026-08-11", 1500)])
    docs = [
        paper_doc("A", 1, "AA00000004", S1, "2026-10-01", 4300),
        paper_doc("A", 2, "AA00000005", S1, "2026-10-02", 5000),
        paper_doc("A", 3, "AC00000001", S2, "2026-09-20", 1200),                   # clean paper
        paper_doc("B", 1, "AA00000008", S1, "2026-10-05", 840, buyer=None, doc_type="e_invoice"),
        paper_doc("B", 2, "BB00000099", S2, "2026-08-11", 1500),                    # deferred claim, already filed
        paper_doc("A", 4, "AA00000010", S1, "2026-10-07", 700),                     # voided on the platform
    ]
    paper = ev.paper_result(CLIENTS, docs, pr)
    rep = pc.check_period(CLIENTS, paper=paper, platform_root=pr, filed_root=fr)
    return rep


def _v(rep, vid):
    return next(v for v in rep["vouchers"] if v["id"] == vid)


def test_einvoice_side_rules(firm):
    assert _v(firm, "A/platform/AA00000001") == {"id": "A/platform/AA00000001", "client_id": "A", "side": "e-invoice",
                                                 "outcome": "ready", "period_rules": []}
    assert _v(firm, "A/platform/AA00000002")["period_rules"] == ["DUP_PRIOR_FILING"]
    assert _v(firm, "A/platform/AA00000002")["outcome"] == "held"
    assert _v(firm, "A/platform/AA00000003")["period_rules"] == []          # reused track, another seller
    assert _v(firm, "A/platform/AA00000004")["period_rules"] == ["PLATFORM_AMOUNT_DIFF"]
    assert _v(firm, "A/platform/AA00000005")["period_rules"] == []
    v = _v(firm, "A/platform/AA00000006")
    assert v["period_rules"] == ["S19_NONDEDUCTIBLE"] and v["outcome"] == "ready"   # flagged, code 3, not held
    assert _v(firm, "A/platform/AA00000007")["period_rules"] == ["FIXED_ASSET_HINT"]
    assert _v(firm, "A/platform/AA00000008")["period_rules"] == []          # the platform names A as the buyer
    assert _v(firm, "A/platform/AA00000009")["period_rules"] == ["PLATFORM_RECORD_CONFLICT"]
    assert _v(firm, "A/platform/AA00000010/void")["outcome"] == "left_out"


def test_paper_side_and_outcomes(firm):
    assert _v(firm, "A/2026-10/x1.jpg")["outcome"] == "on_platform"       # its platform line carries the hold
    assert _v(firm, "A/2026-10/x2.jpg")["outcome"] == "on_platform"
    assert _v(firm, "A/2026-09/x3.jpg")["outcome"] == "ready"
    b1 = _v(firm, "B/2026-10/x1.jpg")
    assert b1["period_rules"] == ["DUP_CROSS_CLIENT"] and b1["outcome"] == "held"
    b2 = _v(firm, "B/2026-08/x2.jpg")
    assert b2["period_rules"] == ["DUP_PRIOR_FILING"] and b2["outcome"] == "held"
    a4 = _v(firm, "A/2026-10/x4.jpg")
    assert "VOID_ON_PLATFORM" in a4["existing"] and a4["outcome"] == "held"
    e = next(x for x in firm["exceptions"] if x["voucher"] == "B/2026-10/x1.jpg")
    assert e["related_docs"] == ["A/platform/AA00000008"] and e["source"] == "paper"


def test_summary_counts_and_line(firm):
    s = firm["summary"]
    assert (s["einvoices"], s["paper"], s["einvoice_voided_left_out"]) == (10, 6, 1)
    assert s["held"] == {"einvoice": 3, "paper": 3, "total": 6}
    assert s["ready"]["einvoice"] == 7 and s["paper_on_platform"] == 2
    assert s["einvoices_with_item_names"] == 2
    assert s["line"].startswith("This period: 10 e-invoices (from the platform download) + 6 paper vouchers (read), "
                                "6 held for a person")
    assert any("without item names" in x for x in firm["not_checked"])
    assert firm["by_client"]["B"]["held"]["paper"] == 2


def test_every_period_exception_has_a_citation(firm):
    for e in firm["exceptions"]:
        assert e["citations"] and all(c["label"] for c in e["citations"]), e["rule_id"]


def test_aggregate_only_leaks_no_numbers(firm):
    txt = json.dumps(pc.aggregate_only(firm), ensure_ascii=False)
    for s in ("AA0000", "BB0000", A_UBN, B_UBN, S1, S2, "x1.jpg"):
        assert s not in txt


def test_without_paper_or_history(tmp_path):
    write(tmp_path / A_UBN / f"{A_UBN}.txt", [rec(A_UBN, S1, "AA00000001", "2026-09-03", 1000)])
    rep = pc.check_period({"A": CLIENTS["A"]}, platform_root=tmp_path, filed_root=tmp_path / "none")
    assert rep["summary"]["einvoices"] == 1 and rep["summary"]["ready"]["total"] == 1
    assert rep["rules_run"]["paper"] == []


def test_real_mode_refuses_output_inside_repo(tmp_path):
    with pytest.raises(SystemExit):
        ev.main(["--ubn", A_UBN, "--period", "2026-09/2026-10", "--platform-root", str(tmp_path),
                 "--out", str(ev.ROOT / "eval" / "x.json")])


# ---------------------------------------------------------------------------------------------------- tax at risk

def test_tax_at_risk_on_the_firm(firm):
    a = firm["by_client"]["A"]["tax_at_risk"]
    # held: AA00000002 prior filing (100) + AA00000009 record conflict (45) + paper x4 voided on the platform (35)
    # are wrong claims; AA00000004 photo and platform differ (200) is unconfirmed
    assert (a["wrong_claim"], a["unconfirmed"], a["vouchers_wrong_claim"], a["vouchers_unconfirmed"]) == (180, 200, 3, 1)
    assert a["input_tax_claimed"] == 5730 and a["tax_unknown"] == 0
    assert a["above_2000"] is False and a["above_5pct"] is True        # 380 / 5,730 = 6.6%
    b = firm["by_client"]["B"]["tax_at_risk"]
    # x1: a consumer-style photo of A's invoice, no tax on it: counted as a voucher, NT$0, tax unknown
    assert (b["wrong_claim"], b["vouchers_wrong_claim"], b["tax_unknown"]) == (75, 2, 1)
    t = firm["summary"]["tax_at_risk"]
    assert (t["wrong_claim"], t["unconfirmed"], t["clients_above_2000"], t["clients_above_5pct"]) == (255, 200, 0, 2)
    assert "above_2000" not in t                                      # fine limits are per business, not per firm
    assert [r["client_id"] for r in firm["risk_ranking"]] == ["A", "B"]
    agg = pc.aggregate_only(firm)["tax_at_risk"]
    assert agg["wrong_claim"] == 255 and agg["clients_above_5pct"] == 2


def _hv(i, tax, outcome="held", side="e-invoice", filed=True, cid="A"):
    return {"id": f"v{i}", "client_id": cid, "side": side, "outcome": outcome, "tax": tax, "filed": filed,
            "period_rules": []}


def _ex(vid, rule, severity="high", cid="A"):
    return {"voucher": vid, "client_id": cid, "rule_id": rule, "severity": severity}


def test_tax_split_wrong_and_unconfirmed():
    vs = [_hv(1, 1000, "ready"), _hv(2, 300), _hv(3, 70), _hv(4, 50, side="paper"), _hv(5, 20, side="paper"),
          _hv(6, 999, "on_platform", side="paper"), _hv(7, 40, side="paper", filed=False)]
    excs = [_ex("v2", "DUP_PRIOR_FILING"), _ex("v3", "PLATFORM_AMOUNT_DIFF", "hold")]
    paper_exc = {"v4": [_ex("v4", "READ_REVIEW", "review"), _ex("v4", "S19_NONDEDUCTIBLE", "medium")],
                 "v5": [_ex("v5", "READ_REVIEW", "review"), _ex("v5", "S19_NONDEDUCTIBLE", "warning")]}
    r = pc._tax_at_risk(vs, excs, paper_exc)
    # v2 and v4 wrong (a medium Art. 19 finding); v3 and v5 unconfirmed (a "to confirm" warning is not a finding);
    # v6 is booked from its platform line; v7 is held but not in the media file: a voucher, NT$0
    assert (r["wrong_claim"], r["unconfirmed"]) == (350, 90)
    assert (r["vouchers_wrong_claim"], r["vouchers_unconfirmed"]) == (2, 3)
    assert r["input_tax_claimed"] == 1000 + 300 + 70 + 50 + 20


@pytest.mark.parametrize("wrong, above", [(2000, False), (2001, True)])
def test_flag_2000_boundary(wrong, above):
    r = pc._tax_at_risk([_hv(1, 1_000_000, "ready"), _hv(2, wrong)], [_ex("v2", "DUP_CROSS_MONTH")], {})
    assert r["wrong_claim"] == wrong and r["above_2000"] is above


@pytest.mark.parametrize("held, above", [(500, False), (501, True)])
def test_flag_5pct_boundary(held, above):
    r = pc._tax_at_risk([_hv(1, 10_000 - held, "ready"), _hv(2, held)], [], {})
    assert r["input_tax_claimed"] == 10_000 and r["unconfirmed"] == held
    assert r["above_5pct"] is above and r["above_2000"] is False


def test_paper_tax_none_counts_as_voucher():
    r = pc._tax_at_risk([_hv(1, None, side="paper"), _hv(2, None, "ready", side="paper")], [], {})
    assert (r["input_tax_claimed"], r["unconfirmed"], r["vouchers_unconfirmed"], r["tax_unknown"]) == (0, 0, 1, 2)
    assert r["above_5pct"] is False


def test_risk_ranking_order():
    cl = {c: {"id": c} for c in ("A", "B", "C", "D")}
    vs = [_hv(1, 100, cid="A"), _hv(2, 500, cid="B"), _hv(3, 500, cid="C"), _hv(4, 900, cid="C"),
          _hv(5, 10, "ready", cid="D")]
    excs = [_ex("v1", "DUP_CROSS_CLIENT", cid="A"), _ex("v2", "DUP_PRIOR_FILING", cid="B"),
            _ex("v3", "VOID_ON_PLATFORM", cid="C")]
    rep = pc._report(cl, vs, excs, {}, False, {})
    rk = rep["risk_ranking"]
    assert [r["client_id"] for r in rk] == ["C", "B", "A", "D"]     # 500 + 900 unconfirmed beats 500 + 0
    assert rk[0] == {"client_id": "C", "wrong_claim": 500, "unconfirmed": 900, "input_tax_claimed": 1400,
                     "above_2000": False, "above_5pct": True}
