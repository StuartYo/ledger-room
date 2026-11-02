"""heldout2 (layout family C, added 2026-11-02 for total reading) was frozen and committed before any reader change.

Same checks as the first two sets: the committed images are the ones the answers were written for, the rule engine on
the ground-truth fields reproduces the answers, and the set shares no seed, layout family or client with the others.
"""
import hashlib
import json
from pathlib import Path

from ledger_room import rules
from synth.generate import to_rule_doc

ROOT = Path(__file__).resolve().parents[1]


def _exp(name):
    return json.loads((ROOT / "eval" / f"expected_{name}.json").read_text(encoding="utf-8"))


def test_heldout2_images_match_frozen_hashes():
    exp = _exp("heldout2")
    assert exp["synthetic"] is True and exp["layout_family"] == "C"
    for d in exp["docs"]:
        p = ROOT / "data" / "heldout2" / "clients" / d["doc_id"]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == d["sha256"], d["doc_id"]


def test_heldout2_oracle_reproduces_answers():
    exp = _exp("heldout2")
    clients = json.loads((ROOT / "data" / "heldout2" / "clients.json").read_text(encoding="utf-8"))
    cmap = {c["id"]: c for c in clients["clients"]}
    docs = [to_rule_doc(d["doc_id"], d["client_id"], d["month"], d["truth"]) for d in exp["docs"]]
    got = rules.issues_by_doc(rules.run_rules(docs, cmap))
    for d in exp["docs"]:
        assert got.get(d["doc_id"], set()) == set(d["issues"]), d["doc_id"]


def test_heldout2_is_separate_and_has_the_total_traps():
    c = _exp("heldout2")
    for other in ("main", "heldout"):
        o = _exp(other)
        assert c["seed"] != o["seed"] and c["layout_family"] != o["layout_family"]
        assert not ({d["client_id"] for d in c["docs"]} & {d["client_id"] for d in o["docs"]})
    near = [d["printed_near_total"] for d in c["docs"]]
    cash = [p for p in near if p.get("payment", {}).get("kind") == "cash"]
    assert len(cash) >= 10 and all(p["payment"]["paid"] > 0 and p["payment"]["change"] > 0 for p in cash)
    assert sum(1 for p in near if p.get("deposit")) >= 3
