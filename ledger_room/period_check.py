"""Period check: every input voucher of a client's filing period, e-invoices and paper together, one report.

Before a VAT return goes out, a bookkeeper has, per client and period:
  - the MOF e-invoice platform's media filing download (einvoice_platform.load_client): every e-invoice issued to the
    client, no item names;
  - optionally a carrier export with item names (einvoice.read_platform_csv), placed next to the download;
  - the paper vouchers, photographed and read (a pipeline result: pipeline.audit or its result.json);
  - the media files the firm already filed for earlier periods (the history).
check_period() runs one set of checks across all of them and says, per voucher, whether it can be booked as it is
("ready"), must wait for a person ("held"), is booked from its platform line instead of the photo ("on_platform"),
or is a voided platform line that is left out ("left_out").

Checks added here (rules.RULES has the citations):
  DUP_PRIOR_FILING          an invoice about to be claimed (a platform line, or a paper line that would be filed)
                            whose number and seller are already in an earlier filed media file (VAT Act Art. 51(1)(5)).
                            Invoice tracks are reused across years, so the seller UBN must match too; without a seller
                            UBN on either side the number alone decides.
  PLATFORM_AMOUNT_DIFF      a photographed invoice whose number is on the client's own download (IN_PLATFORM) but whose
                            total or seller differs: the platform line is held. Skipped when the photo's total is
                            already unconfirmed (it went to a person as READ_REVIEW).
  PLATFORM_RECORD_CONFLICT  one invoice number twice in the client's downloads with different amounts (identical
                            re-downloads are merged by einvoice_platform.load_client).
  DUP_CROSS_CLIENT          extended: an invoice on one client's download (the platform says who the buyer is) that
                            also turns up in another client's paper, or on two clients' downloads.
  S19_NONDEDUCTIBLE         on platform lines that have item names in a carrier export (same keyword table as paper).
  FIXED_ASSET_HINT          on platform lines with a net of NT$80,000 or more (Audit Regulations Art. 77-1).
The paper side keeps every rule of the normal run (rules.run_rules) and gets the first two and DUP_CROSS_CLIENT too.

Not checked, and why (also in the report under "not_checked"):
  - VAT Act Art. 19 on platform lines without a carrier export: the media file carries no item names.
  - B2B exchange invoices the buyer has not confirmed: the media file layout has no confirmation field.
  - UBN checksums on platform lines: the numbers come from the platform, not from a reading.
  - Allowances (formats 23 / 24) are carried into the filing as deductions; they are not matched to their invoice.
  - Whether a photographed e-invoice SHOULD be on the download: upload delays and B2B confirmation make an absence
    weak evidence, so it is not reported.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from . import einvoice, export, rules
from . import einvoice_platform as plat

ROOT = Path(__file__).resolve().parents[1]
FILED_ROOT = ROOT / "data" / "filed"
NEW_BLOCKING = {"DUP_PRIOR_FILING", "PLATFORM_AMOUNT_DIFF", "PLATFORM_RECORD_CONFLICT"}
EINVOICE_RULES = ["PLATFORM_RECORD_CONFLICT", "DUP_PRIOR_FILING", "PLATFORM_AMOUNT_DIFF", "DUP_CROSS_CLIENT",
                  "S19_NONDEDUCTIBLE", "FIXED_ASSET_HINT"]
PAPER_EXTRA_RULES = ["DUP_PRIOR_FILING", "DUP_CROSS_CLIENT against other clients' platform lines"]


# ---------------------------------------------------------------------------------------------------- inputs

def load_filed(client, *, root: Path | None = None, files=None) -> dict:
    """Input lines of the media files this client already filed for EARLIER periods: every *.txt under
    <root>/<client UBN>/ (root defaults to data/filed). Put only earlier periods' files there.
    Returns {"files", "input": [records], "errors"}: issued input records with a voucher number, buyer = the client."""
    ubn = plat._client_ubn(client)
    if files is None:
        base = Path(root or FILED_ROOT) / ubn
        files = sorted(p for p in base.rglob("*") if p.is_file() and p.suffix.lower() == ".txt") if base.exists() else []
    out = {"files": [str(f) for f in files], "input": [], "errors": []}
    for f in files:
        recs, errs = plat.read_media_file(Path(f))
        out["errors"] += errs
        out["input"] += [r for r in recs if r["side"] == "input" and r["status"] == "issued"
                         and (r["buyer_ubn"] in (ubn, None)) and (r["invoice_no"] or r["other_voucher_no"])]
    return out


def load_carrier(client, *, root: Path | None = None, files=None) -> list[einvoice.EInvoice]:
    """Carrier exports (*.csv) next to the client's platform download: <root>/<client UBN>/**/*.csv."""
    ubn = plat._client_ubn(client)
    if files is None:
        base = Path(root or plat.PLATFORM_ROOT) / ubn
        files = sorted(p for p in base.rglob("*") if p.is_file() and p.suffix.lower() == ".csv") if base.exists() else []
    invs: list[einvoice.EInvoice] = []
    for f in files:
        invs += einvoice.read_platform_csv(f)[0]
    return invs


def _total(r: dict) -> int:
    return r["amount"] if r.get("tax_inclusive") else r["amount"] + r["tax"]


def _net(r: dict) -> int:
    if r.get("tax_inclusive"):
        return r["amount"] - (r["amount"] - round(r["amount"] / 1.05))
    return r["amount"]


def _vkey(r: dict) -> str | None:
    return r.get("invoice_no") or r.get("other_voucher_no") or r.get("customs_no")


# ---------------------------------------------------------------------------------------------------- the check

def _exc(voucher: dict, rule_id: str, severity: str, message: str, related=(), citations=None, fields=()) -> dict:
    e = rules.Exception_(voucher["id"], voucher["client_id"], rule_id, severity, message, list(fields),
                         related_docs=list(related), citations=citations).to_dict()
    e["source"] = voucher["side"]
    e["voucher"] = voucher["id"]
    if voucher.get("image"):
        e["image"] = voucher["image"]
    e["evidence"] = []
    return e


def check_period(clients, *, paper: dict | None = None, platform_root: Path | None = None,
                 filed_root: Path | None = None, platform: dict | None = None, filed: dict | None = None,
                 carrier: dict | None = None) -> dict:
    """clients: {id: client} or a list (each with "id", "ubn", "period"). paper: a pipeline result (result.json) for the
    same clients, or None. platform / filed / carrier: {client_id: ...} to pass data in directly; otherwise read from
    platform_root (default data/platform) and filed_root (default data/filed). Returns the report dict."""
    cl = clients if isinstance(clients, dict) else {c["id"]: c for c in clients}
    vouchers: list[dict] = []
    excs: list[dict] = []
    per_client_info: dict[str, dict] = {}

    # ---- e-invoice side
    filed_by: dict[str, dict] = {}
    for cid, c in cl.items():
        p = (platform or {}).get(cid) or plat.load_client(c, c.get("period"), root=platform_root)
        fl = (filed or {}).get(cid) or load_filed(c, root=filed_root)
        filed_by[cid] = fl
        car = (carrier or {}).get(cid)
        if car is None:
            car = load_carrier(c, root=platform_root)
        car_by_no = {i.invoice_no: i for i in car if not i.voided}
        info = {"platform_files": len(p["files"]), "platform_errors": len(p["errors"]), "filed_files": len(fl["files"]),
                "filed_input_lines": len(fl["input"]), "carrier_invoices": len(car_by_no),
                "carrier_not_in_download": 0}
        groups: dict[str, list[dict]] = defaultdict(list)
        allowances: list[dict] = []
        for r in p["input"]:
            if r["is_allowance"]:
                allowances.append(r)
            elif _vkey(r):
                groups[_vkey(r)].append(r)
        for k, recs in groups.items():
            r = recs[0]
            v = {"id": f"{cid}/platform/{k}", "client_id": cid, "side": "e-invoice", "invoice_no": r["invoice_no"],
                 "seller_ubn": r["seller_ubn"], "buyer_ubn": r["buyer_ubn"], "month": r["month"],
                 "format_code": r["format_code"], "total": _total(r), "net": _net(r),
                 "tax": r["tax"] if not r.get("tax_inclusive") else _total(r) - _net(r),
                 "records": len(recs), "items": None, "filed": True}
            vouchers.append(v)
            if len({(x["amount"], x["tax"], x["seller_ubn"]) for x in recs}) > 1:
                excs.append(_exc(v, "PLATFORM_RECORD_CONFLICT", "hold",
                                 f"Invoice {k} appears {len(recs)} times in the platform downloads with different "
                                 "amounts or sellers (" + "; ".join(f"{x['month']} NT${_total(x):,} from {x['seller_ubn']}"
                                                                    for x in recs)
                                 + "). Only one may be filed: check which download is current.", fields=["invoice_no"]))
            inv = car_by_no.get(r["invoice_no"]) if r["invoice_no"] else None
            if inv is not None and inv.lines:
                v["items"] = [l.desc for l in inv.lines]
                d = einvoice.to_rule_doc(inv, cid, buyer_ubn=cl[cid]["ubn"])
                cat, kw, conf = rules.s19_category(d)
                if cat and (r.get("deduction") in (None, "1", "2")) and v["tax"] > 0:
                    art = next(a for cc, a, _ in rules.S19_LEXICON if cc == cat)
                    why = f"keyword '{kw}'" if kw else "model-suggested category"
                    if kw in rules.TO_CONFIRM_KW:
                        excs.append(_exc(v, "S19_NONDEDUCTIBLE", "warning",
                                         f"To confirm: input tax NT${v['tax']:,} on e-invoice {k} is not deductible if "
                                         f"this was entertainment or a staff benefit (Art. 19(1) {art}); {why} in the "
                                         "carrier export. Deductible if bought for resale or as a business cost.",
                                         fields=["items"]))
                    else:
                        excs.append(_exc(v, "S19_NONDEDUCTIBLE", "medium",
                                         f"Input tax NT${v['tax']:,} on e-invoice {k} is not deductible: Art. 19(1) "
                                         f"{art}; {why} in the carrier export. File it with deduction code 3.",
                                         fields=["items"]))
            fa = rules.fixed_asset_reason(v["net"], v["items"] or [])
            if fa:
                excs.append(_exc(v, "FIXED_ASSET_HINT", "warning",
                                 f"E-invoice {k} may be a fixed asset ({fa}): confirm deduction code 2 (or 4 if "
                                 "non-deductible). Code left unchanged.", fields=["net"]))
        for i, r in enumerate(allowances, 1):
            vouchers.append({"id": f"{cid}/platform/{_vkey(r)}/allowance" + (f"{i}" if i > 1 else ""),
                             "client_id": cid, "side": "e-invoice", "kind": "allowance", "invoice_no": r["invoice_no"],
                             "seller_ubn": r["seller_ubn"], "total": _total(r), "filed": True})
        for r in p["void"]:
            if r["side"] == "input":
                vouchers.append({"id": f"{cid}/platform/{_vkey(r)}/void", "client_id": cid, "side": "e-invoice",
                                 "kind": "void", "invoice_no": r["invoice_no"], "seller_ubn": r["seller_ubn"],
                                 "filed": False})
        dl_nos = {k for k in groups}
        info["carrier_not_in_download"] = sum(1 for n in car_by_no if n not in dl_nos)
        info["carrier_matched"] = sum(1 for n in car_by_no if n in dl_nos)
        per_client_info[cid] = info

    ev_by_client_no = {(v["client_id"], v["invoice_no"]): v for v in vouchers
                       if v["side"] == "e-invoice" and not v.get("kind") and v["invoice_no"]}

    # ---- paper side
    paper_rows = {}
    paper_exc: dict[str, list[dict]] = defaultdict(list)
    if paper is not None:
        for e in paper.get("exceptions", []):
            paper_exc[e["doc_id"]].append(e)
        paper_rows = {r["doc_id"]: r for r in export.rows(paper)}
        for d in paper["docs"]:
            if d["client_id"] not in cl:
                continue
            f = d.get("fields") or {}
            ids = {e["rule_id"] for e in paper_exc[d["doc_id"]]}
            row = paper_rows.get(d["doc_id"], {})
            utility = "UTILITY_BILL_PLATFORM" in ids
            v = {"id": d["doc_id"], "client_id": d["client_id"], "side": "paper", "image": d.get("image"),
                 "invoice_no": None if utility else rules.norm_invoice_no(f.get("invoice_no")),
                 "seller_ubn": f.get("seller_ubn"), "buyer_ubn": f.get("buyer_ubn"), "total": f.get("total"),
                 "net": f.get("net"), "tax": f.get("tax"), "status": row.get("status"), "existing": sorted(ids),
                 "filed": row.get("status") in ("READY", "HOLD")}
            vouchers.append(v)
            # PLATFORM_AMOUNT_DIFF: photo vs the client's own platform line
            if "IN_PLATFORM" in ids and v["invoice_no"]:
                ev = ev_by_client_no.get((v["client_id"], v["invoice_no"]))
                total_unconfirmed = any(e["rule_id"] == "READ_REVIEW" and "total" in e.get("evidence_fields", [])
                                        for e in paper_exc[d["doc_id"]])
                if ev is not None and not total_unconfirmed:
                    diffs = []
                    if v["total"] is not None and v["total"] != ev["total"]:
                        diffs.append(f"total NT${v['total']:,} on the paper, NT${ev['total']:,} on the platform")
                    if v["seller_ubn"] and ev["seller_ubn"] and v["seller_ubn"] != ev["seller_ubn"]:
                        diffs.append(f"seller UBN {v['seller_ubn']} on the paper, {ev['seller_ubn']} on the platform")
                    if diffs:
                        excs.append(_exc(ev, "PLATFORM_AMOUNT_DIFF", "hold",
                                         f"Invoice {ev['invoice_no']}: " + "; ".join(diffs) + ". The platform line "
                                         "is held until the paper and the seller are checked.", related=[v["id"]],
                                         fields=["total", "seller_ubn"]))

    # ---- DUP_PRIOR_FILING (both sides): only vouchers that would be filed this period
    for v in vouchers:
        if not v.get("filed") or v.get("kind") or not v.get("invoice_no"):
            continue
        hits = [r for r in filed_by.get(v["client_id"], {}).get("input", []) if r["invoice_no"] == v["invoice_no"]]
        same = [r for r in hits if not v["seller_ubn"] or not r["seller_ubn"] or r["seller_ubn"] == v["seller_ubn"]]
        if same:
            r = same[0]
            sure = bool(v["seller_ubn"] and r["seller_ubn"])
            excs.append(_exc(v, "DUP_PRIOR_FILING", "high",
                             f"Invoice {v['invoice_no']}" + (f" from {v['seller_ubn']}" if sure else "")
                             + f" is already in a filed media file ({Path(r['file']).parent.name}/"
                             f"{Path(r['file']).name}, data month {r['month']}, NT${_total(r):,})"
                             + ("" if sure else " (number only: no seller UBN to compare)")
                             + ". Claiming it again would claim the same input tax twice.",
                             fields=["invoice_no", "seller_ubn"]))

    # ---- DUP_CROSS_CLIENT with at least one platform line (paper-paper pairs are already in the paper run)
    by_no: dict[str, list[dict]] = defaultdict(list)
    for v in vouchers:
        if v.get("kind") or not v.get("invoice_no"):
            continue
        if v["side"] == "paper" and "IN_PLATFORM" in v.get("existing", []):
            continue          # the photo stands for its own client's platform line, which is in the index already
        by_no[v["invoice_no"]].append(v)
    for no, members in by_no.items():
        plats = [m for m in members if m["side"] == "e-invoice"]
        if len({m["client_id"] for m in members}) < 2 or not plats:
            continue
        for m in members:
            # a platform line is only questioned by another client's platform line (the platform names the buyer);
            # a photo is questioned by any other client's platform line
            owners = [p for p in plats if p["client_id"] != m["client_id"]
                      and (not m["seller_ubn"] or not p["seller_ubn"] or m["seller_ubn"] == p["seller_ubn"])]
            if not owners:
                continue
            own = ", ".join(sorted({f"{o['client_id']} (buyer UBN {o['buyer_ubn']})" for o in owners}))
            what = ("Both downloads name a buyer: check which client the invoice belongs to"
                    if m["side"] == "e-invoice" else
                    "The platform names that client as the buyer, so this photo is not this client's voucher")
            excs.append(_exc(m, "DUP_CROSS_CLIENT", "high",
                             f"Invoice {no} is on the e-invoice platform download of client {own}. {what}; booking it "
                             "here as well would count the same purchase twice.",
                             related=[o["id"] for o in owners],
                             citations=rules.CITE_DUP_VAT_CROSS if m.get("filed") else rules.CITE_DUP_INCOME,
                             fields=["invoice_no", "seller_ubn", "total"]))

    # ---- outcomes
    exc_by_v: dict[str, list[dict]] = defaultdict(list)
    for e in excs:
        exc_by_v[e["voucher"]].append(e)

    def blocking(e: dict) -> bool:
        return (e["rule_id"] in export.BLOCKING or e["rule_id"] in NEW_BLOCKING
                or (e["rule_id"] == "PERIOD_MISMATCH" and e["severity"] != "warning"))

    for v in vouchers:
        new = exc_by_v.get(v["id"], [])
        v["period_rules"] = sorted({e["rule_id"] for e in new})
        if v["side"] == "e-invoice":
            if v.get("kind") == "void":
                v["outcome"] = "left_out"
            else:
                v["outcome"] = "held" if any(blocking(e) for e in new) else "ready"
            continue
        old = paper_exc.get(v["id"], [])
        if any(blocking(e) for e in new):
            v["outcome"] = "held"
        elif v["status"] == "HOLD":
            v["outcome"] = "held"
        elif v["status"] == "READY":
            v["outcome"] = "ready"
        elif {"IN_PLATFORM", "UTILITY_BILL_PLATFORM"} & set(v["existing"]):
            v["outcome"] = "on_platform"
        else:   # expense voucher without input tax: booked as an expense unless something blocks it
            v["outcome"] = "held" if any(blocking(e) for e in old) else "ready"

    return _report(cl, vouchers, excs, per_client_info, paper is not None, paper_exc)


# Rules after which claiming the voucher's input tax would be wrong, not merely unconfirmed. All seven ids exist in
# rules.RULES (the period-check ones are defined there too). Non-deductible items and double claims are not keying
# errors, so the 5% keying-error allowance does not cover them.
WRONG_CLAIM_RULES = {"DUP_PRIOR_FILING", "DUP_CROSS_CLIENT", "DUP_CROSS_MONTH", "S19_NONDEDUCTIBLE", "VOID_ON_PLATFORM",
                     "BUYER_NOT_CLIENT", "PLATFORM_RECORD_CONFLICT"}
# The flags only rank which client to look at first. Being under a limit does not make a claim right.
FLAG_BASIS = {
    "above_2000": "wrong_claim over NT$2,000: Penalty Reduction Standard Art. 15 leaves a VAT Act Art. 51 case "
                  "unfined only at NT$2,000 or less of missed tax a period",
    "above_5pct": "held input tax over 5% of the period's input tax: Penalty Reduction Standard Art. 15 leaves keying "
                  "errors under 5% unfined when filing by media file (non-deductible items and double claims are not "
                  "keying errors)",
}


def _tax_at_risk(vs: list[dict], excs: list[dict], paper_exc: dict) -> dict:
    """Input tax this client would claim, split into what is held because the claim would be wrong and what is held
    because it is not confirmed yet. Deterministic arithmetic on the outcomes above, no model."""
    exc_by_v: dict[str, list[dict]] = defaultdict(list)
    for e in excs:
        exc_by_v[e["voucher"]].append(e)
    claimed = wrong = unconf = n_wrong = n_unconf = unknown = 0
    for v in vs:
        if v.get("kind") or v["outcome"] not in ("ready", "held"):
            continue
        if v["outcome"] == "ready" and not v.get("filed"):
            continue                       # an expense voucher without input tax: nothing claimed
        tax = v.get("tax")
        if tax is None:
            unknown += 1
        t = (tax or 0) if v.get("filed") else 0
        claimed += t
        if v["outcome"] != "held":
            continue
        # a "to confirm" warning (e.g. an Art. 19 keyword that may be a business cost) is unconfirmed, not wrong
        is_wrong = any(e["rule_id"] in WRONG_CLAIM_RULES and e.get("severity") != "warning"
                       for e in exc_by_v.get(v["id"], []) + paper_exc.get(v["id"], []))
        if is_wrong:
            wrong += t
            n_wrong += 1
        else:
            unconf += t
            n_unconf += 1
    return {"input_tax_claimed": claimed, "wrong_claim": wrong, "unconfirmed": unconf,
            "vouchers_wrong_claim": n_wrong, "vouchers_unconfirmed": n_unconf, "tax_unknown": unknown,
            "above_2000": wrong > 2000,
            "above_5pct": claimed > 0 and (wrong + unconf) * 20 > claimed,      # integer form of > 5%
            "flag_basis": FLAG_BASIS}


def _counts(vs: list[dict], excs: list[dict], paper_exc: dict) -> dict:
    ein = [v for v in vs if v["side"] == "e-invoice"]
    pap = [v for v in vs if v["side"] == "paper"]
    oc = lambda xs, o: sum(1 for v in xs if v["outcome"] == o)   # noqa: E731
    held_by: dict[str, int] = defaultdict(int)
    ids = {v["id"] for v in vs}
    for v in vs:
        if v["outcome"] != "held":
            continue
        rs = set(v["period_rules"]) | {e["rule_id"] for e in paper_exc.get(v["id"], [])
                                       if e["rule_id"] in export.BLOCKING
                                       or (e["rule_id"] == "PERIOD_MISMATCH" and e["severity"] != "warning")}
        for r in rs:
            if r in export.BLOCKING or r in NEW_BLOCKING or r == "PERIOD_MISMATCH":
                held_by[r] += 1
    ein_v = [v for v in ein if not v.get("kind")]
    return {
        "einvoices": len(ein_v), "einvoice_allowances": sum(1 for v in ein if v.get("kind") == "allowance"),
        "einvoice_voided_left_out": oc(ein, "left_out"),
        "einvoices_with_item_names": sum(1 for v in ein_v if v.get("items")),
        "paper": len(pap),
        "held": {"einvoice": oc(ein, "held"), "paper": oc(pap, "held"), "total": oc(ein, "held") + oc(pap, "held")},
        "ready": {"einvoice": oc(ein, "ready"), "paper": oc(pap, "ready"), "total": oc(ein, "ready") + oc(pap, "ready")},
        "paper_on_platform": oc(pap, "on_platform"),
        "flagged_not_held": sum(1 for v in vs if v["outcome"] == "ready" and v.get("period_rules")),
        "held_by_rule": dict(sorted(held_by.items())),
        "period_exceptions_by_rule": dict(sorted(
            {r: sum(1 for e in excs if e["rule_id"] == r and e["voucher"] in ids) for r in
             {e["rule_id"] for e in excs}}.items())),
        "tax_at_risk": _tax_at_risk(vs, excs, paper_exc),
    }


def _report(cl: dict, vouchers: list[dict], excs: list[dict], info: dict, has_paper: bool, paper_exc: dict) -> dict:
    tot = _counts(vouchers, excs, paper_exc)
    by_client = {cid: {**_counts([v for v in vouchers if v["client_id"] == cid],
                                 [e for e in excs if e["client_id"] == cid], paper_exc), **info.get(cid, {})}
                 for cid in cl}
    # the fine limits apply per business and period, so the whole-firm block counts flagged clients instead of
    # carrying flags of its own
    tr = tot["tax_at_risk"]
    for k in ("above_2000", "above_5pct", "flag_basis"):
        tr.pop(k)
    tr["clients_above_2000"] = sum(1 for b in by_client.values() if b["tax_at_risk"]["above_2000"])
    tr["clients_above_5pct"] = sum(1 for b in by_client.values() if b["tax_at_risk"]["above_5pct"])
    risk_ranking = sorted(
        ({"client_id": cid, **{k: b["tax_at_risk"][k] for k in ("wrong_claim", "unconfirmed", "input_tax_claimed",
                                                                 "above_2000", "above_5pct")}}
         for cid, b in by_client.items()),
        key=lambda r: (-r["wrong_claim"], -r["unconfirmed"], r["client_id"]))
    line = (f"This period: {tot['einvoices']} e-invoices (from the platform download) + {tot['paper']} paper vouchers "
            f"(read), {tot['held']['total']} held for a person, {tot['ready']['total']} ready to book"
            + (f", {tot['paper_on_platform']} photo{'' if tot['paper_on_platform'] == 1 else 's'} booked from "
               "their platform line" if tot["paper_on_platform"] else "")
            + ".")
    n_no_items = tot["einvoices"] - tot["einvoices_with_item_names"]
    return {
        "summary": {**tot, "line": line, "clients": len(cl),
                    "platform_files": sum(i["platform_files"] for i in info.values()),
                    "filed_files": sum(i["filed_files"] for i in info.values()),
                    "carrier_invoices": sum(i["carrier_invoices"] for i in info.values())},
        "rules_run": {
            "e-invoice": EINVOICE_RULES,
            "paper": (sorted(k for k in rules.RULES if k not in NEW_BLOCKING and k != "UBN_NOT_REGISTERED")
                      + PAPER_EXTRA_RULES) if has_paper else [],
        },
        "not_checked": [
            f"VAT Act Art. 19 keywords on {n_no_items} e-invoice line(s) without item names: the media file has none "
            "(a carrier export next to the download adds them)",
            "B2B exchange invoices the buyer has not confirmed: the media file layout has no confirmation field",
            "UBN checksums on platform lines: the numbers come from the platform, not from a reading",
            "allowances (formats 23 / 24): carried into the filing as deductions, not matched to their invoice",
            "whether a photographed e-invoice should be on the download: upload delays and B2B confirmation make an "
            "absence weak evidence",
        ],
        "by_client": by_client,
        # who to look at first before the filing deadline: wrong claims first, then unconfirmed tax
        "risk_ranking": risk_ranking,
        "flag_basis": FLAG_BASIS,
        "exceptions": excs,
        "vouchers": [{k: v[k] for k in ("id", "client_id", "side", "outcome", "period_rules") if k in v}
                     | ({"kind": v["kind"]} if v.get("kind") else {})
                     | ({"existing": v["existing"]} if v.get("existing") else {}) for v in vouchers],
    }


def aggregate_only(report: dict) -> dict:
    """The report without invoice numbers, voucher ids, messages or per-voucher lines: for real client data."""
    s = {k: v for k, v in report["summary"].items() if k != "line"}
    return {"privacy": "aggregates only: no invoice numbers, UBNs, voucher ids, messages or single transactions",
            "summary": s, "tax_at_risk": dict(s["tax_at_risk"]), "rules_run": report["rules_run"],
            "not_checked": [x for x in report["not_checked"]],
            "clients": len(report["by_client"])}
