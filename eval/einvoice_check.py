"""Real-data check of the e-invoice import path (no OCR) on platform CSV exports.

python -m eval.einvoice_check FILE.csv [FILE.csv ...] --out DIR [--label TEXT]

Writes DIR/einvoice_check.json with aggregates only (counts, rates, timing): no invoice numbers, seller names,
item names or single transactions. Use it on personal or client exports that must not leave the machine; keep DIR
outside the repository.

What it tests:
  * import: line items grouped into invoices; discount (negative) and zero-price lines; voided invoices;
  * UBN checksum on every real seller UBN (2023 rule): a positive control, every real number should pass;
    also counts how many would FAIL the pre-2023 mod-10 rule (what an outdated checker would wrongly reject);
  * duplicate detection (same invoice number, or same seller + date + amount): expected about zero;
  * Art. 19 / expense classification with the same keyword tables the rule engine uses, and the S19 rule itself
    run "as if each invoice had been issued to a company" (consumer invoices carry no buyer UBN).
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from ledger_room import einvoice, rules
from ledger_room import ubn as ubnlib

AS_IF_UBN = "00501503"   # a valid test UBN (python-stdnum example), used only for the what-if S19 run


def check(paths: list[str], label: str) -> dict:
    t0 = time.perf_counter()
    invs, st = [], Counter()
    for p in paths:
        i, s = einvoice.read_platform_csv(p)
        invs += i
        st.update(s)
    t_import = time.perf_counter() - t0
    live = [i for i in invs if not i.voided]
    sellers = {i.seller_ubn for i in live if i.seller_ubn}
    chk = {u: ubnlib.check(u) for u in sellers}
    n_inv_ubn = sum(1 for i in live if i.seller_ubn)
    months = sorted({f"{i.date:%Y-%m}" for i in live})

    client = {"id": "P", "name": "(one person's consumer invoices)", "ubn": AS_IF_UBN,
              "period": [min(i.date for i in live).isoformat(), max(i.date for i in live).isoformat()], "policy": {}}
    t1 = time.perf_counter()
    docs = [einvoice.to_rule_doc(i, "P") for i in live]
    plain = rules.run_rules(docs, {"P": client})
    t_rules = time.perf_counter() - t1
    asif = rules.run_rules([einvoice.to_rule_doc(i, "P", buyer_ubn=AS_IF_UBN) for i in live], {"P": client})

    s19 = Counter()
    exp = Counter()
    for d in docs:
        cat, kw, conf = rules.s19_category(d)
        s19[cat or "none"] += 1
        ecat, _ = rules.expense_category(d)
        exp[ecat or "none"] += 1
    by_rule = lambda ex: dict(Counter(e.rule_id for e in ex))   # noqa: E731
    return {
        "label": label,
        "privacy": "aggregates only; no invoice numbers, sellers, items or single transactions",
        "months": len(months),
        "files": len(paths),
        "csv_rows": st["rows"], "footnote_or_blank_rows_skipped": st["skipped_rows"],
        "line_items": st["line_items"],
        "invoices": len(invs), "voided": len(invs) - len(live),
        "invoices_with_buyer_ubn": sum(1 for i in live if i.buyer_ubn),
        "invoices_with_discount_lines": sum(1 for i in live if i.discount_lines),
        "invoices_with_zero_price_lines": sum(1 for i in live if i.zero_lines),
        "negative_invoice_amounts": sum(1 for i in live if (i.amount or 0) < 0),
        "non_integer_amounts": sum(1 for i in live if i.amount is not None and i.amount != int(i.amount)),
        "invoice_amount_basis": dict(Counter(i.amount_basis for i in live)),
        "distinct_sellers": len(sellers),
        "seller_ubn_pass_2023_rule": f"{sum(c.valid_new for c in chk.values())}/{len(sellers)}",
        "invoices_seller_ubn_pass": f"{sum(1 for i in live if i.seller_ubn and chk[i.seller_ubn].valid_new)}/{n_inv_ubn}",
        "sellers_failing_old_mod10_rule": sum(1 for c in chk.values() if c.valid_new and not c.valid_old),
        "rule_exceptions_as_consumer": by_rule(plain),
        "duplicates_found": sum(1 for e in plain if e.rule_id.startswith("DUP_")),
        "rule_exceptions_as_if_issued_to_a_company": by_rule(asif),
        "art19_keyword_category": dict(s19),
        "expense_category": dict(exp),
        "seconds_import_total": round(t_import, 4),
        "ms_per_invoice_import_plus_rules": round((t_import + t_rules) * 1000 / max(1, len(live)), 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="e-invoice platform export")
    a = ap.parse_args()
    r = check(a.files, a.label)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "einvoice_check.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
