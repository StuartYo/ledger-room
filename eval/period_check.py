"""Period check: e-invoices from the platform download and paper vouchers, one report per filing period.

Synthetic measurement (SYNTHETIC data, deterministic rules, no model):
  python -m eval.period_check --synthetic
    reads data/period_synthetic/ (synth/period_check.py), scores against eval/expected_period_check_synthetic.json
    (written by the generator before the checker existed) and writes eval/results_period_check.json.
    The paper side starts from fields as if read correctly: reading accuracy is measured by the other evaluations.

Real data (aggregates only; keep every input and the output OUTSIDE the repository):
  python -m eval.period_check --ubn 12345678 --period 2026-09/2026-10 --platform-root DIR [--filed-root DIR]
                              [--paper RESULT.json] --out OUT.json
    DIR/<UBN>/**/*.txt  the platform's media filing download (and any carrier *.csv next to it)
    --filed-root        DIR/<UBN>/**/*.txt: media files already filed for EARLIER periods (optional)
    --paper             a pipeline result.json for the same client (optional)
  Writes only counts: no invoice numbers, UBNs, voucher ids or messages.
"""
from __future__ import annotations

import argparse
import calendar
import json
import subprocess
import time
from collections import Counter
from pathlib import Path

from ledger_room import einvoice_platform as plat
from ledger_room import period_check as pc
from ledger_room import pipeline, rules

ROOT = Path(__file__).resolve().parents[1]
SYN = ROOT / "data" / "period_synthetic"
KEY = ROOT / "eval" / "expected_period_check_synthetic.json"
RESULT = ROOT / "eval" / "results_period_check.json"
CONFIRMED = "ocr-confirmed"


def paper_result(clients: dict, docs: list[dict], platform_root: Path) -> dict:
    """A pipeline-shaped result for paper vouchers whose fields are given (no reading): the same rule engine and the
    same platform hooks as pipeline.audit(platform="auto")."""
    issued, void = plat.platform_hooks_by_client(clients, root=platform_root)
    records = {cid: plat.load_client(c, c.get("period"), root=platform_root)["input"] for cid, c in clients.items()}
    rdocs = [pipeline.to_rule_doc(d, d["fields"], {k: CONFIRMED for k in d["fields"]}) for d in docs]
    excs = rules.run_rules(rdocs, clients, platform_invoices=issued, platform_void=void, platform_records=records)
    return {"synthetic": True, "clients": list(clients.values()),
            "docs": [{**d, "image": None, "tax_voucher_kind": r.tax_voucher_kind,
                      "small_business_receipt": r.small_business_receipt} for d, r in zip(docs, rdocs)],
            "exceptions": [e.to_dict() for e in excs]}


def git_rev() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def score(report: dict, key: dict) -> dict:
    got = {v["id"]: v for v in report["vouchers"]}
    keyed = {k["voucher"]: k for k in key["vouchers"]}
    period_ids = set(pc.EINVOICE_RULES) | pc.NEW_BLOCKING | {"DUP_CROSS_CLIENT", "VOID_ON_PLATFORM"}
    cats: dict[str, dict] = {}
    for k in key["vouchers"]:
        v = got.get(k["voucher"])
        found = set(v["period_rules"]) | set(v.get("existing", [])) if v else set()
        for cat in k["planted"]:
            c = cats.setdefault(cat, {"planted": 0, "caught": 0, "missed": []})
            c["planted"] += 1
            if v and all(r in found for r in k["expected_rules"]) and v["outcome"] == k["expected_outcome"]:
                c["caught"] += 1
            else:
                c["missed"].append(k["voucher"])
    false_holds = [i for i, k in keyed.items() if i in got and got[i]["outcome"] == "held"
                   and k["expected_outcome"] != "held"]
    missed_holds = [i for i, k in keyed.items() if k["expected_outcome"] == "held"
                    and (i not in got or got[i]["outcome"] != "held")]
    unexpected = []
    for i, v in got.items():
        exp = set(keyed[i]["expected_rules"]) if i in keyed else set()
        extra = sorted((set(v["period_rules"]) | (set(v.get("existing", [])) & period_ids)) - exp)
        if extra:
            unexpected.append({"voucher": i, "rules": extra})
    return {
        "by_category": {c: {"planted": x["planted"], "caught": x["caught"], "missed": len(x["missed"]),
                            "missed_vouchers": x["missed"]} for c, x in sorted(cats.items())},
        "planted_total": sum(x["planted"] for x in cats.values()),
        "caught_total": sum(x["caught"] for x in cats.values()),
        "outcomes": {"expected": dict(Counter(k["expected_outcome"] for k in key["vouchers"])),
                     "got": dict(Counter(v["outcome"] for v in report["vouchers"]))},
        "false_holds": len(false_holds), "false_hold_vouchers": false_holds,
        "missed_holds": len(missed_holds), "missed_hold_vouchers": missed_holds,
        "unexpected_flags": len(unexpected), "unexpected_flag_detail": unexpected,
        "vouchers_in_key": len(keyed), "vouchers_in_report": len(got),
        "vouchers_not_in_report": sorted(set(keyed) - set(got)),
        "vouchers_not_in_key": sorted(set(got) - set(keyed)),
        "ready_to_book": {"expected": sum(1 for k in key["vouchers"] if k["expected_outcome"] == "ready"),
                          "got": report["summary"]["ready"]["total"],
                          "got_einvoice": report["summary"]["ready"]["einvoice"],
                          "got_paper": report["summary"]["ready"]["paper"]},
    }


def run_synthetic(write: bool = True) -> dict:
    firm = json.loads((SYN / "firm.json").read_text(encoding="utf-8"))
    clients = {c["id"]: c for c in firm["clients"]}
    docs = json.loads((SYN / "paper.json").read_text(encoding="utf-8"))["docs"]
    key = json.loads(KEY.read_text(encoding="utf-8"))
    t0 = time.perf_counter()
    paper = paper_result(clients, docs, SYN / "platform")
    report = pc.check_period(clients, paper=paper, platform_root=SYN / "platform", filed_root=SYN / "filed")
    secs = time.perf_counter() - t0
    s = report["summary"]
    out = {
        "synthetic": True,
        "note": "SYNTHETIC firm period (synth/period_check.py). Rules are deterministic code, no model. Paper fields "
                "are given as if read correctly; reading accuracy is measured by the other evaluations. The answer key "
                "was committed (d93141e) before the checker existed and was not edited.",
        "first_run": True, "code_rev": git_rev(), "key": "eval/expected_period_check_synthetic.json",
        "clients": s["clients"], "einvoices": s["einvoices"], "einvoice_allowances": s["einvoice_allowances"],
        "einvoice_voided_left_out": s["einvoice_voided_left_out"], "paper": s["paper"],
        "einvoices_with_item_names": s["einvoices_with_item_names"],
        "held": s["held"], "ready": s["ready"], "paper_on_platform": s["paper_on_platform"],
        "flagged_not_held": s["flagged_not_held"], "held_by_rule": s["held_by_rule"],
        "summary_line": s["line"],
        "rules_run": report["rules_run"], "not_checked": report["not_checked"],
        **score(report, key),
        "seconds_total": round(secs, 3),
    }
    if write:
        RESULT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def run_real(a) -> dict:
    months = plat.period_months(a.period)
    y, m = map(int, months[-1].split("-"))
    client = {"id": "C", "ubn": a.ubn,
              "period": [months[0] + "-01", f"{months[-1]}-{calendar.monthrange(y, m)[1]:02d}"]}
    paper = None
    if a.paper:
        paper = json.loads(Path(a.paper).read_text(encoding="utf-8"))
        paper["clients"] = [dict(c, id=c["id"]) for c in paper["clients"] if c.get("ubn") == a.ubn]
        ids = {c["id"] for c in paper["clients"]}
        paper["docs"] = [d for d in paper["docs"] if d["client_id"] in ids]
        paper["exceptions"] = [e for e in paper.get("exceptions", []) if e["client_id"] in ids]
        if paper["clients"]:
            client["id"] = paper["clients"][0]["id"]
    report = pc.check_period({client["id"]: client}, paper=paper, platform_root=Path(a.platform_root),
                             filed_root=Path(a.filed_root) if a.filed_root else Path(a.platform_root) / "_none")
    out = pc.aggregate_only(report)
    out["label"] = a.label
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--ubn")
    ap.add_argument("--period", help="YYYY-MM/YYYY-MM")
    ap.add_argument("--platform-root")
    ap.add_argument("--filed-root")
    ap.add_argument("--paper")
    ap.add_argument("--out")
    ap.add_argument("--label", default="real platform download")
    a = ap.parse_args(argv)
    if a.synthetic:
        out = run_synthetic()
        print(json.dumps({k: out[k] for k in ("summary_line", "planted_total", "caught_total", "false_holds",
                                               "missed_holds", "unexpected_flags", "ready_to_book")},
                         ensure_ascii=False, indent=1))
        return
    if not (a.ubn and a.period and a.platform_root and a.out):
        ap.error("real data needs --ubn, --period, --platform-root and --out (or use --synthetic)")
    out_path = Path(a.out).resolve()
    if out_path.is_relative_to(ROOT):
        ap.error("write real-data output outside the repository")
    out = run_real(a)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
