"""The period-check paragraph of README.md must quote eval/results_period_check.json, and the measurement must hold."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
R = json.loads((ROOT / "eval" / "results_period_check.json").read_text(encoding="utf-8"))
KEY = json.loads((ROOT / "eval" / "expected_period_check_synthetic.json").read_text(encoding="utf-8"))


def _has(s):
    assert s in README, f"README does not say {s!r} (taken from eval/results_period_check.json)"


def test_readme_quotes_the_results_file():
    assert R["synthetic"] is True and R["first_run"] is True
    _has(f"first run, code {R['code_rev']}; synthetic data, deterministic rules, no model")
    _has(f"{R['clients']} clients, {R['einvoices']} e-invoices from platform downloads ({R['einvoice_allowances']} "
         f"allowances and {R['einvoice_voided_left_out']} voided lines besides) and {R['paper']} paper vouchers")
    _has(f"Planted errors caught: {R['caught_total']} of {R['planted_total']} (")
    for k, v in R["by_category"].items():
        _has(f"{k} {v['caught']}/{v['planted']}")
    _has(f"Vouchers held that should not have been: {R['false_holds']}; planted holds missed: {R['missed_holds']}; "
         f"rules raised where nothing was planted: {R['unexpected_flags']}")
    rb = R["ready_to_book"]
    _has(f"Ready to book: {rb['got']} ({rb['got_einvoice']} e-invoices, {rb['got_paper']} paper; the key says "
         f"{rb['expected']}), {R['held']['total']} held for a person")
    for rid in ("DUP_PRIOR_FILING", "PLATFORM_AMOUNT_DIFF", "PLATFORM_RECORD_CONFLICT"):
        _has(f"| `{rid}` (")
    _has("python -m eval.period_check --synthetic")


def test_results_match_the_frozen_key_and_a_rerun():
    """The key is what the generator planted; the stored first run must agree with it and with a fresh run."""
    assert R["planted_total"] == sum(KEY["planted"].values())
    assert R["vouchers_in_key"] == len(KEY["vouchers"]) and R["vouchers_not_in_report"] == []
    from eval import period_check as ev
    again = ev.run_synthetic(write=False)
    for k in ("caught_total", "false_holds", "missed_holds", "unexpected_flags", "ready_to_book", "held",
              "by_category", "einvoices", "paper"):
        assert again[k] == R[k], k
