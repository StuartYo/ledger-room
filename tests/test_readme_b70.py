"""The B70 numbers in README.md and docs/ must quote the B70 results files (no model needed).

The full tables live in docs/b70_measurements.md, docs/real_data.md and docs/evaluation.md; the README shows a summary. Every file is
checked: a number may appear in any of them, and no stale statement may appear in any of them."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "eval"
FILES = [ROOT / "README.md", ROOT / "docs" / "b70_measurements.md", ROOT / "docs" / "real_data.md",
         ROOT / "docs" / "evaluation.md"]   # evaluation.md: the Stage II targets and their B70 result
README = "\n\n".join(p.read_text(encoding="utf-8") for p in FILES)


def _load(name):
    p = EV / name
    if not p.exists():
        pytest.skip(f"{name} not in this checkout")
    return json.loads(p.read_text(encoding="utf-8"))


def _has(s):
    assert s in README, f"README.md / docs do not say {s!r} (taken from the B70 results files)"


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


def test_real_company_strict_and_evidence():
    """Evidence block, strict re-scores and the real company box quote their results files."""
    tw = _load("results_real_tw_strict.json")["conditions"]
    d, s = tw["default"], tw["strict"]
    _has(f"Default reading at this code: {d['fields_wrong']}/{d['fields_scored']} fields wrong, "
         f"{d['photos_sent_to_a_person']} photos sent to a person, {d['wrong_and_not_held']} photos with a wrong field not stopped")
    _has(f"Strict reading: {s['photos_sent_to_a_person']} sent to a person, {s['wrong_and_not_held']} not stopped")
    _has(f"real_tw default {d['photos_sent_to_a_person']} sent, {d['wrong_and_not_held']} let through, "
         f"strict {s['photos_sent_to_a_person']} sent, {s['wrong_and_not_held']} let through")
    t2 = _load("results_real_tw2_strict.json")["conditions"]
    assert t2["strict"]["wrong_and_not_held"] == 0 and s["wrong_and_not_held"] == 0
    _has(f"{t2['strict']['photos_sent_to_a_person']} sent to a person\nwith strict reading, "
         f"{t2['default']['wrong_and_not_held']} and {t2['default']['photos_sent_to_a_person']} with the default")
    b2 = {k: _load(f)["conditions"]["strict"] for k, f in (("tw", "results_real_tw_strict_2b.json"), ("tw2", "results_real_tw2_strict_2b.json"))}
    s2 = t2["strict"]
    _has(f"| {b2['tw']['fields_wrong']} of {b2['tw']['fields_scored']} / {b2['tw']['photos_sent_to_a_person']} / {b2['tw']['wrong_and_not_held']} "
         f"| {s['fields_wrong']} of {s['fields_scored']} / {s['photos_sent_to_a_person']} / {s['wrong_and_not_held']} |")
    _has(f"| {b2['tw2']['fields_wrong']} of {b2['tw2']['fields_scored']} / {b2['tw2']['photos_sent_to_a_person']} / {b2['tw2']['wrong_and_not_held']} "
         f"| {s2['fields_wrong']} of {s2['fields_scored']} / {s2['photos_sent_to_a_person']} / {s2['wrong_and_not_held']} |")
    rc = _load("results_real_company.json")
    ol = rc["old_ledger_vs_ledger_room"]
    o, l9 = ol["old_ledger"], ol["ledger_room_strict"]["Qwen3.5-9B-int4-ov"]
    wf = o["wrong_fields"]
    _has(f"**{o['wrong_rows']} wrong rows** (invoice\n   number {wf['invoice_no']}, date {wf['date']}, total {wf['total']})")
    _has(f"**{l9['read_wrong']} misread, all {l9['read_wrong']} held, {l9['wrong_and_not_held']} let through**;\n"
         f"   {l9['sent_to_a_person']} of {l9['vouchers']} sent to a person")
    l2 = ol["ledger_room_strict"]["Qwen3.5-2B-int4-ov"]
    _has(f"| {l2['sent_to_a_person']} / {l2['wrong_and_not_held']} | {l9['sent_to_a_person']} / {l9['wrong_and_not_held']} |")
    fr = rc["first_runs"]
    fe = {m: fr[m]["field_error_rate"]["ledger_room"] for m in fr}
    _has(f"| {fe['Qwen3.5-2B-int4-ov'] * 100:.1f}% | {fe['Qwen3.5-9B-int4-ov'] * 100:.1f}% |")
    for m in ("Qwen3.5-9B-int4-ov", "Qwen3.5-2B-int4-ov"):
        for mode in ("default", "strict"):
            assert f"{fr[m][mode]['sent_to_a_person']} / {fr[m][mode]['wrong_and_not_held']}" in README, (m, mode)
    af = rc["after_fix_utility_routing"]["Qwen3.5-9B-int4-ov"]["strict"]
    _has(f"9B strict: {af['sent_to_a_person']} sent to a person ({fr['Qwen3.5-9B-int4-ov']['strict']['sent_to_a_person']} before), "
         f"{af['wrong_and_not_held_excluding_bills']} wrong and not held outside the\nbills")
    assert README.count("used with the owner's consent") == 1


def test_readme_first_screen_quotes_the_results_files():
    """The README summary tables (first screen) must equal the results files they name."""
    txt = (ROOT / "README.md").read_text(encoding="utf-8")

    def has(s):
        assert s in txt, f"README.md does not say {s!r}"

    ol = _load("results_real_company.json")["old_ledger_vs_ledger_room"]
    l9 = ol["ledger_room_strict"]["Qwen3.5-9B-int4-ov"]
    has(f"{ol['old_ledger']['rows']} real invoices of one company: its old ledger let {ol['old_ledger']['wrong_rows']} "
        f"wrong rows through, Ledger Room let {l9['wrong_and_not_held']}")
    has(f"({l9['read_wrong']} misread, all {l9['read_wrong']} held for a person)")
    tw = _load("results_real_tw_strict.json")["conditions"]["strict"]
    tw2 = _load("results_real_tw2_strict.json")["conditions"]["strict"]
    assert tw["wrong_and_not_held"] == 0 and tw2["wrong_and_not_held"] == 0
    has("58 public real Taiwan photos, strict reading: 0 let through")
    ag = _load("results_agent_b70_strict.json")
    assert ag["strict"] is True and ag["measured_on_b70"] is True
    a = ag["summary"]
    has(f"{a['cleared']} of {a['held']} held vouchers cleared, {a['slipped']} cleared wrong")
    one = _load("results_one_card.json")["peak_card_memory"]["gpu_resident_vram0_gb"]
    ags = _load("results_agent_b70_strict.json")["memory_gb"]["peak"]["gpu_resident_vram0_gb"]
    has(f"{one} GB with both reading, {ags:.1f} GB peak in the agent run (strict reading)")
    assert ags >= _load("results_agent_b70.json")["memory_gb"]["peak"]["gpu_resident_vram0_gb"]   # the higher peak is quoted
    cv = _load("results_same_host_cpu_vs_b70_live.json")["runs"]
    has(f"{cv['GPU.1']['sec_per_doc']['mean']} s a voucher on one B70 vs {cv['CPU']['sec_per_doc']['mean']} s")
    has(f"| {cv['CPU']['docs_per_hour']} vs {cv['GPU.1']['docs_per_hour']:,} | "
        f"{cv['CPU']['sec_per_doc']['median']} s vs {cv['GPU.1']['sec_per_doc']['median']} s |")
    c1 = _load("results_concurrency.json")["rows"][0]
    c2 = _load("results_concurrency_2cards.json")["rows"][0]
    lr = _load("results_longrun_zoompad.json")["rows"][0]
    assert lr["restarts"] == 0 and c1["workers_loaded"] == 1
    for r in (c1, c2, lr):
        has(f"| {r['docs_per_hour']:,} | {r['latency_s']['p50']:.1f} s | {r['latency_s']['p95']:.1f} s |")
        has(f"| {round(r['power']['wh_per_1000_docs'])} | ")
    has(f"two cards for 70 minutes: {lr['docs_per_hour']:,} an hour, {lr['restarts']} restarts")
    has(f"**{round(c1['power']['by_device']['GPU.0']['mean_w'])} W while reading, "
        f"{round(c1['power']['wh_per_1000_docs'])} Wh per 1,000 vouchers**")
    off = _load("results_offline_b70.json")
    assert off["pass"] is True and off["non_local_connections"] == []
    has(f"({off['docs_read']} documents read, {off['duration_s']} s watched)")
    for s in ("UGen workstation", "On the UGen B70", "What it does not do yet", "work in progress", "still off"):
        assert s not in txt, s
