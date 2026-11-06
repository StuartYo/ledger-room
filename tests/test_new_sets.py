"""blind3 (frozen, never run before its one official run) and hard (damaged clean vouchers).

blind3 checks touch only files and synth/blind3.py's own checksum code: no ledger_room code is run on blind3.
Text files are compared with any line ending (git may turn LF into CRLF on checkout); images byte for byte.
"""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _text_sha_ok(p: Path, want: str) -> bool:
    raw = p.read_bytes()
    lf = raw.replace(b"\r\n", b"\n")
    return want in {_sha(raw), _sha(lf), _sha(lf.replace(b"\n", b"\r\n"))}


def _json(p):
    return json.loads((ROOT / p).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ blind3

def test_blind3_frozen_hashes():
    fz = _json("eval/frozen_blind3.json")
    assert fz["synthetic"] is True
    for k in ("generator", "expected", "clients_json"):
        assert _text_sha_ok(ROOT / fz[k]["path"], fz[k]["sha256"]), fz[k]["path"]
    imgs = sorted(p for p in (ROOT / "data" / "blind3" / "clients").rglob("*") if p.is_file())
    got = {str(p.relative_to(ROOT / "data" / "blind3" / "clients")).replace("\\", "/"): _sha(p.read_bytes()) for p in imgs}
    assert got == {k: v["sha256"] for k, v in fz["images"].items()}


def test_blind3_key_matches_freeze_and_is_marked_synthetic():
    exp, fz = _json("eval/expected_blind3.json"), _json("eval/frozen_blind3.json")
    assert exp["synthetic"] is True and _json("data/blind3/clients.json")["synthetic"] is True
    assert {d["doc_id"]: d["sha256"] for d in exp["docs"]} == {k: v["sha256"] for k, v in fz["images"].items()}
    assert all(d["synthetic"] is True for d in exp["docs"])
    assert 40 <= exp["n_docs"] <= 60 and exp["n_clean"] >= 20
    assert all(v >= 3 for v in exp["issue_counts"].values()) and len(exp["issue_counts"]) == 6


def test_blind3_generator_is_independent_of_the_code_under_test():
    src = (ROOT / "synth" / "blind3.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+(ledger_room|synth\.generate|synth import generate)", src, re.M)


def test_blind3_is_new():
    b3 = _json("eval/expected_blind3.json")
    clients = {d["client_id"] for d in b3["docs"]}
    for other in ("main", "heldout", "heldout2"):
        o = _json(f"eval/expected_{other}.json")
        assert b3["seed"] != o["seed"] and b3["layout_family"] != o["layout_family"]
        assert not clients & {d["client_id"] for d in o["docs"]}
        assert not {d["truth"]["invoice_no"] for d in b3["docs"] if d["truth"]["invoice_no"]} & \
            {d["truth"]["invoice_no"] for d in o["docs"] if d["truth"]["invoice_no"]}


def test_blind3_ubn_answers_follow_the_published_checksum():
    from synth.blind3 import ubn_ok
    for d in _json("eval/expected_blind3.json")["docs"]:
        t = d["truth"]
        bad = any(u and not ubn_ok(u, 5) for u in (t["seller_ubn"], t["buyer_ubn"]))
        assert bad == ("UBN_CHECKSUM" in d["issues"]), d["doc_id"]


# ------------------------------------------------------------------ hard

def test_hard_images_match_key():
    exp = _json("eval/expected_hard.json")
    assert exp["synthetic"] is True and _json("data/hard/clients.json")["synthetic"] is True
    for d in exp["docs"]:
        assert _sha((ROOT / "data" / "hard" / "clients" / d["doc_id"]).read_bytes()) == d["sha256"], d["doc_id"]


def test_hard_key_is_complete():
    exp = _json("eval/expected_hard.json")
    assert 40 <= exp["n_docs"] <= 60
    for t in exp["damage_types"]:
        assert sum(d["degradation"]["type"] == t for d in exp["docs"]) >= 5, t
    for d in exp["docs"]:
        assert d["synthetic"] is True and d["issues"] == []
        hr = d["human_readable"]
        assert set(hr) == {f for f in exp["key_fields"] if d["truth"].get(f) is not None}
        assert d["expect_hold"] == (not all(hr.values()))
        assert sorted(f for f, v in hr.items() if not v) == d["degradation"]["fields_affected"]
    assert 5 <= exp["n_expect_hold"] < exp["n_docs"]


def test_hard_vouchers_are_clean_on_the_truth():
    from ledger_room import rules
    from synth.generate import to_rule_doc
    exp = _json("eval/expected_hard.json")
    cmap = {c["id"]: c for c in _json("data/hard/clients.json")["clients"]}
    docs = [to_rule_doc(d["doc_id"], d["client_id"], d["month"], d["truth"]) for d in exp["docs"]]
    assert rules.issues_by_doc(rules.run_rules(docs, cmap)) == {}
