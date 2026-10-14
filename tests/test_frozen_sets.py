"""The expected answers were frozen (committed) before any model/prompt tuning.

These tests make sure (1) the committed images are exactly the ones the answers
were written for, and (2) the deterministic rule engine, fed the ground-truth
fields, reproduces the frozen answers (oracle control = 100%).
"""
import hashlib
import json
from pathlib import Path

import pytest

from ledger_room import rules
from synth.generate import to_rule_doc

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("set_name", ["main", "heldout"])
def test_images_match_frozen_hashes(set_name):
    exp = json.loads((ROOT / "eval" / f"expected_{set_name}.json").read_text(encoding="utf-8"))
    assert exp["synthetic"] is True
    for d in exp["docs"]:
        p = ROOT / "data" / set_name / "clients" / d["doc_id"]
        assert p.exists(), d["doc_id"]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == d["sha256"], d["doc_id"]


@pytest.mark.parametrize("set_name", ["main", "heldout"])
def test_oracle_rules_reproduce_answers(set_name):
    exp = json.loads((ROOT / "eval" / f"expected_{set_name}.json").read_text(encoding="utf-8"))
    clients = json.loads((ROOT / "data" / set_name / "clients.json").read_text(encoding="utf-8"))
    cmap = {c["id"]: c for c in clients["clients"]}
    docs = [to_rule_doc(d["doc_id"], d["client_id"], d["month"], d["truth"]) for d in exp["docs"]]
    got = rules.issues_by_doc(rules.run_rules(docs, cmap))
    for d in exp["docs"]:
        assert got.get(d["doc_id"], set()) == set(d["issues"]), d["doc_id"]


def test_sets_are_separate():
    a = json.loads((ROOT / "eval" / "expected_main.json").read_text(encoding="utf-8"))
    b = json.loads((ROOT / "eval" / "expected_heldout.json").read_text(encoding="utf-8"))
    assert a["seed"] != b["seed"] and a["layout_family"] != b["layout_family"]
    assert len(a["docs"]) >= 40 and len(b["docs"]) >= 40
    ca = {d["client_id"] for d in a["docs"]}
    cb = {d["client_id"] for d in b["docs"]}
    assert len(ca) >= 5 and len(cb) >= 5 and not (ca & cb)
