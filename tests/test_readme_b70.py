"""The B70 section of README.md must quote the B70 results files (no model needed)."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "eval"
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _load(name):
    p = EV / name
    if not p.exists():
        pytest.skip(f"{name} not in this checkout")
    return json.loads(p.read_text(encoding="utf-8"))


def _has(s):
    assert s in README, f"README does not say {s!r} (taken from the B70 results files)"


def test_stale_b70_statements_are_gone():
    for s in ["No Arc Pro B70 has been used yet", "No B70 number exists yet", "planned, not measured",
              "Nothing in this section has run on an Arc Pro B70", "B70 performance is a plan"]:
        assert s not in README, s
    _has("<!-- r9 after-fix rows go here -->")


def test_heldout_and_targets():
    r9 = _load("results_b70.json")["results"]["Qwen3.5-9B-int4-ov"]
    r27 = _load("results_b70.json")["results"]["Qwen3.5-27B-int4-ov"]
    m = r9["ledger_room"]["micro"]
    _has(f"micro F1 {m['f1']:.2f} ({m['tp']} of {m['tp'] + m['fn']} planted issues, {m['fp']} false alarms)")
    _has(f"key fields wrong {r9['reading']['fields_wrong']}/{r9['reading']['fields_compared']}")
    _has(f"{r9['ledger_room']['sec_per_doc']['mean']} s per document (mean)")
    _has(f"Same 9B auditing on its own: F1 {r9['pure_model']['micro']['f1']:.2f}")
    _has(f"{r27['ledger_room']['sec_per_doc']['mean']} s per document against {r9['ledger_room']['sec_per_doc']['mean']} s")
    _has(f"F1 {r27['pure_model']['micro']['f1']:.2f}, reported")
    t = {(x["model"], x["target"]): x for x in _load("results_b70.json")["targets"]}
    assert t[("Qwen3.5-9B-int4-ov", "Read time per document, goal (mean s)")]["pass"] is False
    _has("**misses the 3 s goal**")
    assert t[("Qwen3.5-27B-int4-ov", "Key fields wrong, held-out")]["pass"] is False
    _has("0/319: **missed**")


def test_hard_set_both_rows():
    h = _load("results_hard.json")["readers"]["Qwen3.5-9B-int4-ov + OCR"]["overall"]
    _has(f"{h['fields_wrong']} of {h['fields_compared']} key fields a person can read")
    _has(f"({h['fields_missing']} left blank")
    _has(f"{h['docs_held']} of {h['docs']} sent to a person (code 8a0fc91)")
    assert h["wrong_not_held"] == 0
    t = _load("results_tiered.json")["sets"]["hard"]
    f = t["first_only"]
    _has(f"{f['key_fields_wrong']}/{f['key_fields_compared']} key fields wrong, {f['docs_for_person']} of {f['docs']} sent")
    assert f["key_fields_compared"] - h["fields_compared"] == 37
    tt = t["tiered"]
    _has(f"{tt['sent_to_second']} documents went to the 27B and {sum(tt['fields_taken_from_second'].values())} fields")
    assert (tt["docs_for_person"], tt["key_fields_wrong"]) == (f["docs_for_person"], f["key_fields_wrong"])


def test_blind3_and_reloads():
    sets = _load("results_tiered.json")["sets"]
    b = sets["blind3"]["first_only"]
    m = b["rule_micro"]
    _has(f"micro F1 {b['rule_f1']} (found {m['tp']}, missed {m['fn']}, {m['fp']} false alarms)")
    _has(f"key fields wrong {b['key_fields_wrong']}/{b['key_fields_compared']}")
    pf = b["key_fields_per_field"]
    _has(f"buyer UBN {pf['buyer_ubn']}, net {pf['net']}, tax {pf['tax']}, total {pf['total']}")
    _has(f"{b['docs_for_person']} of {b['docs']} sent to a person")
    assert sets["blind3"]["code_rev"] == "77fa568"
    rates = [x[m]["docs_per_hour"] for x in sets.values() for m in ("first_only", "tiered")
             if "docs_per_hour" in (x.get(m) or {}) and x.get("first_restart_every")]
    excl = [x[m]["docs_per_hour_excluding_first_reloads"] for x in sets.values() for m in ("first_only", "tiered")
            if "docs_per_hour" in (x.get(m) or {}) and x.get("first_restart_every")]
    _has(f"one card read {min(rates)} to {max(rates)} documents an hour")
    _has(f"leaving reload time out, {min(excl)} to {max(excl)}")


def test_dual_card_real_photos_offline():
    g0 = _load("results_dualcard_gpu0.json")["sets"]["heldout"]["first_only"]
    g1 = _load("results_dualcard_gpu1.json")["sets"]["heldout"]["first_only"]
    assert g0 and g1
    _has(f"{g0['docs_per_hour']:,} + {g1['docs_per_hour']:,} documents per hour")
    tw = _load("results_real_tw_b70.json")["Qwen3.5-9B-int4-ov"]["conditions"]
    lr, mo, oc = tw["ledger_room"], tw["model_only"], tw["ocr_only"]
    _has(f"{lr['fields_wrong']}/{lr['fields_scored']} fields wrong, {lr['photos_sent_to_a_person']} photos sent to a person, "
         f"{lr['wrong_and_not_held']} photos with a wrong field not stopped")
    _has(f"{mo['fields_wrong']}/{mo['fields_scored']} wrong, {mo['wrong_and_not_held']} photos with a wrong field")
    _has(f"{oc['fields_wrong']}/{oc['fields_scored']} wrong, {oc['photos_sent_to_a_person']} photos sent to a person")
    co = _load("results_real_intl_cord_b70.json")["Qwen3.5-9B-int4-ov"]
    c = co["conditions"]
    _has(f"{c['model_only']['total_wrong_or_missing']} of {co['receipts']} totals wrong")
    om = c["ocr_plus_model"]
    _has(f"{om['wrong_and_not_held']} wrong and not stopped, {om['sent_to_a_person']} sent to a person "
         f"({om['held_but_right']} of those")
    off = _load("results_offline_b70.json")
    assert off["pass"] is True
    _has(f"{off['docs_read']} documents read live, {off['duration_s']} s watched")
