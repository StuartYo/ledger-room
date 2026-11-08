"""A person corrects a field the reader got wrong (web app, review screen).

The machine reading in result.json is never changed. Each correction is an entry in the run's review log
(runs/<run>/review.jsonl, the same hash-chained, HMAC-signed AuditLog as the decisions):

    {"event": "correct", "doc_id", "field", "old": <machine reading>, "new": <value typed>, "previous": <value shown
     before this entry>, "user", "role", "note"}

A correction back to the machine reading undoes it (the field is no longer listed as corrected); the entry stays in
the log. apply() lays the corrections over a result and checks the rules again: the corrected document gets the
person's values (provenance "person", which counts as a confirmed source), and its exceptions are the stored ones
plus what the rule engine raises now and minus what it no longer raises (rules.run_rules on the documents rebuilt
from the result, once with and once without the corrections; the difference is what the corrections changed, so
inputs result.json does not keep, like the OCR text, cancel out). The export then uses the corrected values.
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

from . import rules

EDITABLE = ("invoice_no", "date", "seller_ubn", "buyer_ubn", "net", "tax", "total")
AMOUNTS = ("net", "tax", "total")
PERSON = "person"           # provenance of a value a person typed (pipeline.STRICT_CONFIRMED includes it)


class Invalid(ValueError):
    """The typed value cannot be a value of that field; .code names the problem for the page."""

    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code = code


def normalize(field: str, raw) -> str | int | None:
    """The typed value in the form result.json keeps; "" or None clears the field. Raises Invalid."""
    if field not in EDITABLE:
        raise Invalid("field", f"{field} cannot be corrected here")
    if raw is None:
        return None
    s = str(raw).strip()
    if len(s) > 40:
        raise Invalid("format", "too long")
    if s == "":
        return None
    if field == "invoice_no":
        t = re.sub(r"[\s\-]", "", s).upper()
        if not re.fullmatch(r"[A-Z]{2}\d{8}", t):
            raise Invalid("invoice_no", "an invoice number is two letters and eight digits, e.g. AB12345678")
        return t
    if field in ("seller_ubn", "buyer_ubn"):
        t = re.sub(r"\s", "", s)
        if not re.fullmatch(r"\d{8}", t):
            raise Invalid("ubn", "a tax ID (統一編號) is eight digits")
        return t
    if field in AMOUNTS:
        t = re.sub(r"^(NT\$|NTD|\$)", "", s.replace(" ", ""), flags=re.I).replace(",", "").replace("，", "")
        if not re.fullmatch(r"\d{1,10}", t):
            raise Invalid("amount", "an amount is a whole number of NT dollars, e.g. 4652 or 4,652")
        return int(t)
    # date: 2026-09-14, 2026/9/14, 2026.09.14, ROC 115/09/14, 115-9-14, 1150914, 20260914
    m = re.fullmatch(r"(\d{2,4})[\-/.年](\d{1,2})[\-/.月](\d{1,2})日?", s)
    if m:
        y, mo, d = map(int, m.groups())
    elif re.fullmatch(r"\d{7}", s):
        y, mo, d = int(s[:3]), int(s[3:5]), int(s[5:])
    elif re.fullmatch(r"\d{8}", s):
        y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:])
    else:
        raise Invalid("date", "a date is year-month-day, e.g. 2026-09-14 or 115/09/14")
    if y < 1000:
        y += 1911       # ROC year
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        raise Invalid("date", "no such date") from None


def state(entries) -> dict[str, dict[str, dict]]:
    """{doc_id: {field: {"old", "new", "user", "ts", "note"}}} from review-log entries (dicts), oldest first.
    A correction back to the machine reading ("new" == "old") removes the field."""
    out: dict[str, dict[str, dict]] = {}
    for e in entries:
        if e.get("event") != "correct" or not e.get("doc_id") or e.get("field") not in EDITABLE:
            continue
        doc = out.setdefault(e["doc_id"], {})
        if e.get("new") == e.get("old"):
            doc.pop(e["field"], None)
        else:
            doc[e["field"]] = {k: e.get(k) for k in ("old", "new", "user", "ts", "note")}
        if not doc:
            out.pop(e["doc_id"], None)
    return out


def read_state(review_log: Path) -> dict[str, dict[str, dict]]:
    if not review_log.exists():
        return {}
    from .pipeline import AuditLog
    entries = []
    for line in AuditLog.lines(review_log):
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return state(entries)


# ---------------- checking the rules again ----------------

def _rule_docs(result: dict, corr: dict[str, dict[str, dict]] | None) -> list[rules.Doc]:
    from . import pipeline
    out = []
    for d in result.get("docs", []):
        if d.get("read_failed") or d.get("client_id") not in {c["id"] for c in result.get("clients", [])}:
            continue
        f, p = dict(d.get("fields") or {}), dict(d.get("provenance") or {})
        for k, c in ((corr or {}).get(d["doc_id"]) or {}).items():
            f[k], p[k] = c["new"], PERSON
        rd = pipeline.to_rule_doc(d, f, p)
        # read from the OCR text at audit time and kept in result.json
        rd.small_business_receipt = bool(d.get("small_business_receipt"))
        rd.tax_voucher_kind = d.get("tax_voucher_kind")
        out.append(rd)
    return out


def _key(e) -> tuple:
    if isinstance(e, dict):
        return e.get("doc_id"), e.get("rule_id"), e.get("severity"), tuple(e.get("evidence_fields") or ())
    return e.doc_id, e.rule_id, e.severity, tuple(e.evidence_fields)


def _hooks(clients: dict):
    """The platform download and the business register, as the web app's own check used them (pipeline.audit with
    platform="auto", registry=load_default()); missing data simply means none."""
    kw = {}
    try:
        from . import einvoice_platform
        issued, void = einvoice_platform.platform_hooks_by_client(clients)
        kw.update(platform_invoices=issued, platform_void=void,
                  platform_records={c["id"]: einvoice_platform.load_client(c, c.get("period"))["input"]
                                    for c in clients.values()})
    except Exception:
        pass
    if "reg" not in _REG:
        try:
            from .registry import load_default
            _REG["reg"] = load_default()
        except Exception:
            _REG["reg"] = None
    if _REG["reg"] is not None:
        kw["registry"] = _REG["reg"]
    return kw


_REG: dict = {}     # the business register is read once per process


def _evidence(e: rules.Exception_, doc: dict) -> list[dict]:
    from .pipeline import EVIDENCE
    out = []
    for f in EVIDENCE.get(e.rule_id) or e.evidence_fields:
        box = (doc.get("boxes") or {}).get(f)
        if not box:
            continue
        v = doc["fields"].get(f)
        if f == "items":
            v = "; ".join(i.get("desc") or "" for i in v or [])
        out.append({"field": f, "value": v, "box": box, "crop": None, "provenance": doc["provenance"].get(f)})
    return out


def apply(result: dict, corr: dict[str, dict[str, dict]]) -> dict:
    """A copy of result with the corrections applied and the rules checked again (module docstring)."""
    if not corr or "docs" not in result:
        return result
    out = copy.deepcopy(result)
    docs = {d["doc_id"]: d for d in out["docs"]}
    corr = {k: v for k, v in corr.items() if k in docs and v}
    if not corr:
        return result
    for doc_id, fields in corr.items():
        d = docs[doc_id]
        d.setdefault("provenance", {})
        for f, c in fields.items():
            d["fields"][f] = c["new"]
            d["provenance"][f] = PERSON
        d["corrections"] = fields
    clients = {c["id"]: c for c in out.get("clients", [])}
    hooks = _hooks(clients)
    before = Counter(_key(e) for e in rules.run_rules(_rule_docs(result, None), clients, **hooks))
    now_list = rules.run_rules(_rule_docs(result, corr), clients, **hooks)
    now = Counter(_key(e) for e in now_list)
    gone, new = before - now, now - before
    fresh: dict[tuple, list] = {}       # what the rules say now about a corrected document, to refresh the wording
    for e in now_list:
        if e.doc_id in corr:
            fresh.setdefault(_key(e), []).append(e)
    kept = []
    for e in out["exceptions"]:
        k = _key(e)
        if gone[k] > 0:
            gone[k] -= 1
            continue
        if fresh.get(k) and e.get("source") is None:
            ne = fresh[k].pop(0)
            e["message"], e["related_docs"], e["recheck"] = ne.message, ne.related_docs, True
        kept.append(e)
    for e in now_list:
        k = _key(e)
        if new[k] > 0:
            new[k] -= 1
            d = docs[e.doc_id]
            ed = e.to_dict()
            ed.update(evidence=_evidence(e, d), image=d.get("image"), image_size=d.get("image_size"), recheck=True)
            kept.append(ed)
    # evidence values shown under the reasons follow the corrected fields
    for e in kept:
        c = corr.get(e.get("doc_id")) or {}
        for v in e.get("evidence") or []:
            if v.get("field") in c:
                v["value"], v["provenance"] = c[v["field"]]["new"], PERSON
    out["exceptions"] = kept
    s = out.get("summary")
    if isinstance(s, dict):
        s["n_exceptions"] = len(kept)
        s["n_corrected_fields"] = sum(len(v) for v in corr.values())
    return out
