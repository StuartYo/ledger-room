"""eval/agent_real_eval.py: the aggregate counts and the per-voucher leak check, on made-up rows (no real data)."""
import pytest

from eval import agent_real_eval as E

T = {"invoice_no": "AB12345678", "date": "2026-01-02", "seller_ubn": "12345675", "total": 105, "net": 100, "tax": 5,
     "buyer_ubn": None}


def row(f, kind, held, cleared=False, before=None, accepted=None, checks=None, utility=False):
    fb = {**{k: T[k] for k in E.SIX}, **(before or {})}
    r = {"file": f, "kind": kind, "utility_routed": utility, "held_before": held, "fields_before": fb,
         "agent_run": held, "cleared": cleared, "accepted": accepted or {}, "seconds": 10.0 if held else None,
         "steps": 2 if held else 0, "tools": ["zoom", "finish"] if held else [], "checks": checks or {},
         "truth": dict(T), "error": None}
    r["fields_after"] = {**fb, **(accepted or {})} if cleared else fb
    r["held_after"] = held and not cleared
    return r


def test_counts():
    rows = [
        row("a.jpg", "e_invoice", True, True, before={"total": 1}, accepted={"total": 105},
            checks={"total": {"value": 105, "accepted": True, "truth": 105}}),            # cleared right
        row("b.jpg", "e_invoice", True, True, before={"date": "2026-01-03"}, accepted={"total": 105},
            checks={"total": {"value": 105, "accepted": True, "truth": 105}}),            # cleared, date still wrong
        row("c.jpg", "receipt", True, False, before={"tax": 9},
            checks={"tax": {"value": 7, "accepted": False, "truth": 5}}),                 # refused, stays held
        row("d.jpg", "utility_bill", False, before={"total": 2}, utility=True),           # routed, wrong, not held
        row("e.jpg", "three_part_invoice", False, before={"seller_ubn": "1"}),            # wrong, not held
    ]
    g = E.aggregate(rows, {"a.jpg", "e.jpg"})
    w = g["whole_box"]
    assert (w["sent_to_a_person_before"], w["sent_to_a_person_after"], w["cleared"]) == (3, 1, 2)
    assert w["cleared_with_a_wrong_field_six"] == 1 and w["cleared_with_a_wrong_field_three"] == 1
    assert (w["wrong_and_not_held_before_six"], w["wrong_and_not_held_after_six"]) == (2, 3)
    assert g["whole_box_excluding_utility_bills_routed_to_platform"]["wrong_and_not_held_after_six"] == 2
    i = g["invoices_121"]
    assert (i["vouchers"], i["cleared"], i["wrong_and_not_held_after_three"]) == (3, 2, 1)
    p = g["paired_113"]
    assert (p["vouchers"], p["cleared"], p["wrong_and_not_held_after_six"]) == (2, 1, 1)
    a = g["agent"]
    assert (a["proposed_values"], a["proposed_values_wrong"], a["values_refused_by_code"],
            a["wrong_values_refused_by_code"], a["accepted_values_wrong"]) == (3, 1, 1, 1, 0)
    assert g["by_kind"]["e_invoice"]["cleared"] == 2
    assert E.aggregate(rows, None)["paired_113"] is None


def test_leak_check():
    E.check_aggregate_only({"n": 3, "rev": "abc1234"}, ["x.jpg"])
    for bad in ({"f": "x.jpg"}, {"inv": "AB12345678"}, {"ubn": "12345675"}):
        with pytest.raises(SystemExit):
            E.check_aggregate_only(bad, ["x.jpg"])
