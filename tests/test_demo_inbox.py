"""The demo inbox (data/demo, "Sample firm D"): made for screen recordings, not an evaluation set.

What is checked here: the images are the ones the answer key was written for, the rule engine on the ground-truth
fields gives the planted answers, and every UBN printed in the set was looked up in the FIA registration API and found
unregistered (data/demo/ubn_check.json). The registration gate itself is tested with a fake API (no network here).
"""
import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from ledger_room import rules
from ledger_room import ubn as ubnlib
from synth import generate as G

ROOT = Path(__file__).resolve().parents[1]


def _json(p):
    return json.loads((ROOT / p).read_text(encoding="utf-8"))


EXP = _json("eval/expected_demo.json")
CLIENTS = _json("data/demo/clients.json")
CHECK = _json("data/demo/ubn_check.json")


def test_images_match_the_key_and_the_set_is_marked_synthetic():
    assert EXP["synthetic"] is True and CLIENTS["synthetic"] is True and CHECK["synthetic"] is True
    for d in EXP["docs"]:
        p = ROOT / "data" / "demo" / "clients" / d["doc_id"]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == d["sha256"], d["doc_id"]
    on_disk = {str(p.relative_to(ROOT / "data" / "demo" / "clients")).replace("\\", "/")
               for p in (ROOT / "data" / "demo" / "clients").rglob("*.jpg")}
    assert on_disk == {d["doc_id"] for d in EXP["docs"]}


def test_shape_for_the_recording():
    docs = EXP["docs"]
    assert 45 <= len(docs) <= 55 and len(CLIENTS["clients"]) == 5
    kinds = Counter(d["truth"]["doc_type"] for d in docs)
    assert all(kinds[k] >= 1 for k in ("triplicate_invoice", "two_part_register", "e_invoice", "receipt"))
    layouts = Counter(d["layout"] for d in docs)
    assert layouts["hand_triplicate"] >= 5 and layouts["a4_einvoice"] >= 1 and layouts["C"] >= 5
    issues = Counter(i for d in docs for i in d["issues"])
    for t in ("UBN_CHECKSUM", "DUP_CROSS_MONTH", "DUP_CROSS_CLIENT", "PERIOD_MISMATCH"):
        assert issues[t] >= 2, t
    assert any("AMOUNT_MISMATCH" in d["expect_routing"] for d in docs)


def test_rule_engine_on_the_truth_gives_the_planted_answers():
    cmap = {c["id"]: c for c in CLIENTS["clients"]}
    rd = [G.to_rule_doc(d["doc_id"], d["client_id"], d["month"], d["truth"]) for d in EXP["docs"]]
    ex = rules.run_rules(rd, cmap)
    got = rules.issues_by_doc(ex)
    routing = {}
    for e in ex:
        if e.rule_id in ("AMOUNT_MISMATCH", "BUYER_NOT_CLIENT"):
            routing.setdefault(e.doc_id, set()).add(e.rule_id)
    for d in EXP["docs"]:
        assert got.get(d["doc_id"], set()) == set(d["issues"]), d["doc_id"]
        assert routing.get(d["doc_id"], set()) == set(d["expect_routing"]), d["doc_id"]


def test_every_ubn_was_checked_unregistered_and_check_digits_hold():
    printed = {c["ubn"] for c in CLIENTS["clients"]}
    for d in EXP["docs"]:
        printed |= {d["truth"][k] for k in ("seller_ubn", "buyer_ubn") if d["truth"][k]}
    rec = {r["ubn"]: r for r in CHECK["ubns"]}
    assert printed == set(rec) and CHECK["n_ubns"] == len(printed)
    assert CHECK["all_not_found"] is True and all(r["result"] == "not_found" and r["checked_at"] for r in rec.values())
    assert CHECK["api_control"]["result"] == "registered"     # the API answered "registered" for a known number
    planted = {d["truth"][k] for d in EXP["docs"] if "UBN_CHECKSUM" in d["issues"] for k in ("seller_ubn", "buyer_ubn")
               if d["truth"][k] and not ubnlib.is_valid_new(d["truth"][k])}
    assert {u for u in printed if not ubnlib.is_valid_new(u)} == planted and len(planted) == 3


def test_separate_from_the_other_sets():
    mine_c = {d["client_id"] for d in EXP["docs"]}
    mine_u = {u for d in EXP["docs"] for u in (d["truth"]["seller_ubn"], d["truth"]["buyer_ubn"]) if u}
    for other in ("main", "heldout", "heldout2", "blind3", "hard"):
        o = _json(f"eval/expected_{other}.json")
        assert not mine_c & {d["client_id"] for d in o["docs"]}, other
        assert not mine_u & {d["truth"].get(k) for d in o["docs"] for k in ("seller_ubn", "buyer_ubn")}, other
    assert "demo" in _json("web/inboxes.json")["inboxes"]


# ------------------------------------------------------------------ the registration gate, with a fake API

def _fake(registered=(), broken=()):
    calls = []

    def fetch(u):
        calls.append(u)
        if u in broken:
            return 500, "error"
        if u in registered or u == G.REGISTERED_CONTROL:
            return 200, json.dumps({"ban": u, "businessNm": "x"})
        return 404, ""
    return fetch, calls


def test_gate_uses_only_numbers_the_api_does_not_know():
    fetch, calls = _fake(registered={"11111111"}, broken={"22222222"})
    g = G.RegistryGate(fetch=fetch, sleep_s=0)
    g.check_api()
    assert g("11111111") is False and g("22222222") is False and g("33333333") is True
    assert g.rejected_registered == 1 and g.rejected_error == 1
    assert calls.count("22222222") == 3                     # no answer: asked again, then not used
    rep = g.report({"33333333": ["seller"]})
    assert rep["all_not_found"] and [r["ubn"] for r in rep["ubns"]] == ["33333333"]
    assert "11111111" not in json.dumps(rep)                # a registered number is never written down
    with pytest.raises(SystemExit):
        g.report({"44444444": ["buyer"]})                   # printed but never checked


def test_gate_refuses_to_run_when_the_api_says_nothing_is_registered():
    g = G.RegistryGate(fetch=lambda u: (404, ""), sleep_s=0)
    with pytest.raises(SystemExit):
        g.check_api()


def test_planner_draws_again_past_a_registered_number():
    seen = []

    def gate(u):
        seen.append(u)
        return len(seen) > 2                                 # the first two draws are "registered"
    p = G.Planner(1, "E", "t")
    p.ubn_gate = gate
    u = p.ubn("both")
    assert u == seen[-1] and len(seen) == 3 and ubnlib.is_valid_new(u)
    bad = p.bad_variant(u)
    assert not ubnlib.is_valid_new(bad) and seen[-1] == bad
