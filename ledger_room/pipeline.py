"""Batch audit over a firm inbox: data/<set>/clients/<client>/<YYYY-MM>/*.jpg

read (OCR + VLM, cached) -> deterministic rules -> evidence crops -> hash-chained audit log.
Everything runs on the local machine; nothing is sent over the network.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import platform as _platform_mod   # the name "platform" is audit()'s e-invoice platform switch
import subprocess
import threading
import time
from datetime import date, datetime
from pathlib import Path

from PIL import Image

from . import export, keys, rules
from .reader import EXTRACT_PROMPT, TOTAL_ASK_KEY

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "out"
READER_VERSION = "r9"   # bump when post-processing changes so cached reads are redone
PII_RULE = {"label": "Bookkeepers' personal-data safeguard regulation Art. 19: keep personal-data usage records "
                     "and automated-equipment trail data, for at least five years",
            "url": "https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=G0340165&flno=19"}

EVIDENCE = {
    "UBN_CHECKSUM": None,          # decided per exception (seller_ubn / buyer_ubn)
    "DUP_CROSS_MONTH": ["invoice_no", "total", "date"],
    "DUP_CROSS_CLIENT": ["invoice_no", "total", "date"],
    "S19_NONDEDUCTIBLE": ["items", "note"],
    "OVER_LIMIT": ["total"],
    "PERIOD_MISMATCH": ["date", "invoice_period"],
}


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def git_rev() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def load_inbox(set_name: str) -> tuple[dict, list[dict]]:
    base = DATA / set_name
    cfg = json.loads((base / "clients.json").read_text(encoding="utf-8"))
    clients = {c["id"]: c for c in cfg["clients"]}
    docs = []
    for p in sorted((base / "clients").glob("*/*/*.jpg")):
        cid, month = p.parent.parent.name, p.parent.name
        docs.append({"doc_id": f"{cid}/{month}/{p.name}", "client_id": cid, "month": month, "path": p})
    return clients, docs


UNCONFIRMED = {"model-only", "unconfirmed"}


def tax_voucher_kind(f: dict, ocr_lines: list | None = None) -> str | None:
    """transport_ticket / customs_payment (rules.other_voucher_kind) from the OCR text and the read fields."""
    ocr_text = " ".join(l["text"] for l in (ocr_lines or []))
    return rules.other_voucher_kind(ocr_text + " " + rules.fields_text(f), f.get("doc_type"))


def to_rule_doc(d: dict, f: dict, prov: dict | None = None, ocr_lines: list | None = None) -> rules.Doc:
    prov = prov or {}
    ocr_text = " ".join(l["text"] for l in (ocr_lines or []))
    return rules.Doc(
        doc_kind=rules.voucher_kind(ocr_text, rules.norm_invoice_no(f.get("invoice_no"))),
        small_business_receipt=rules.small_business_receipt(ocr_text),
        tax_voucher_kind=tax_voucher_kind(f, ocr_lines),
        uncertain={k: prov[k] for k in ("seller_ubn", "buyer_ubn", "total") if prov.get(k) in UNCONFIRMED},
        doc_id=d["doc_id"], client_id=d["client_id"], month=d["month"], doc_type=f.get("doc_type"),
        invoice_no=rules.norm_invoice_no(f.get("invoice_no")),
        date=date.fromisoformat(f["date"]) if f.get("date") else None,
        invoice_period=f.get("invoice_period"),
        seller_name=f.get("seller_name"), seller_ubn=f.get("seller_ubn"),
        buyer_name=f.get("buyer_name"), buyer_ubn=f.get("buyer_ubn"),
        items=[rules.Item(i.get("desc") or "", i.get("qty"), i.get("amount")) for i in f.get("items", [])],
        net=f.get("net"), tax=f.get("tax"), total=f.get("total"), note=f.get("note"),
        model_category=f.get("category"),
    )


class AuditLog:
    """Append-only JSONL. Each entry carries the SHA-256 of the previous one and an HMAC-SHA256 under the operator key
    (keys.py). The plain hash chain only shows accidental damage: someone who can write the disk can recompute it.
    The HMAC makes an edit, insertion or deletion detectable by whoever holds the key, as long as the key is not on
    the same disk as the log. Neither stops someone who holds both.
    Lines are written ASCII-only (\\uXXXX escapes) so a logged value can never split an entry over two lines; the hashed
    and MAC'd body is unchanged. A MAC'd head record (last seq and hash) kept in the secrets folder makes a removed tail
    detectable: verify_detail(path, key, head=True) adds tail_ok."""

    def __init__(self, path: Path, key: bytes | None = None, append: bool = False):
        self.path = path
        self.key = key if key is not None else keys.audit_key()
        self.prev = "0" * 64
        self.seq = 0
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._rpath = str(path.resolve())
        self.head_path = AuditLog.head_path(path)
        problem = {}
        if append and path.exists() and path.stat().st_size:
            lines = AuditLog.lines(path)
            good = -1
            for i in range(len(lines) - 1, -1, -1):
                try:
                    e = json.loads(lines[i])
                    self.prev, self.seq = str(e["hash"]), int(e["seq"])
                    good = i
                    break
                except (ValueError, KeyError, TypeError):
                    continue
            if good < len(lines) - 1:     # unreadable tail: continue after the last readable entry and say so
                problem["unreadable_lines"] = len(lines) - 1 - good
        elif not (append and path.exists()):
            path.write_text("", encoding="utf-8")
        if append:
            hd = AuditLog._read_head(self.head_path, self.key, self._rpath)
            if hd is False:
                problem["head"] = "head record unreadable or its MAC does not match"
            elif hd is not None and (hd["seq"] > self.seq or (hd["seq"] == self.seq and hd["hash"] != self.prev)):
                problem.update(head_seq=hd["seq"], head_hash=hd["hash"], last_seq=self.seq)
        if problem:
            self.add("log_tail_broken", note="the log on disk does not end where it last ended; continuing after the "
                                             "last readable entry", **problem)

    @staticmethod
    def lines(path: Path) -> list[str]:
        """Entries split on "\\n" only (str.splitlines would also split on U+2028, U+0085 ... inside old lines)."""
        ls = path.read_text(encoding="utf-8", errors="replace").split("\n")
        if ls and ls[-1] == "":
            ls.pop()
        return ls

    @staticmethod
    def head_path(path: Path) -> Path:
        return keys.secrets_dir() / "log_heads" / f"{hashlib.sha256(str(Path(path).resolve()).encode('utf-8')).hexdigest()}.json"

    @staticmethod
    def _head_mac(key: bytes, rpath: str, seq: int, h: str) -> str:
        return hmac.new(key, f"ledger-room log head|{rpath}|{seq}|{h}".encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _read_head(hp: Path, key: bytes, rpath: str):
        """None: no head record. False: unreadable or wrong MAC. Else {"seq", "hash"}."""
        if not hp.exists():
            return None
        try:
            r = json.loads(hp.read_text(encoding="utf-8"))
            seq, h = int(r["seq"]), str(r["hash"])
            if r.get("log") != rpath or not hmac.compare_digest(str(r["mac"]), AuditLog._head_mac(key, rpath, seq, h)):
                return False
            return {"seq": seq, "hash": h}
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _write_head(self) -> None:
        rec = {"log": self._rpath, "seq": self.seq, "hash": self.prev,
               "mac": AuditLog._head_mac(self.key, self._rpath, self.seq, self.prev)}
        try:
            self.head_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.head_path.with_name(f"{self.head_path.name}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(rec), encoding="utf-8")
            tmp.replace(self.head_path)
        except OSError:
            pass    # the log itself is written; a missed head update only weakens the tail check

    def add(self, event: str, **kw) -> None:
        with self.lock:
            self.seq += 1
            e = {"seq": self.seq, "ts": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, **kw,
                 "prev": self.prev}
            body = json.dumps(e, ensure_ascii=False, sort_keys=True).encode("utf-8")
            e["hash"] = hashlib.sha256(body).hexdigest()
            e["mac"] = hmac.new(self.key, body, hashlib.sha256).hexdigest()
            self.prev = e["hash"]
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=True, sort_keys=True) + "\n")
            self._write_head()

    @staticmethod
    def verify_detail(path: Path, key: bytes | None = None, head: bool = False) -> dict:
        """chain_ok: hashes link up. mac_ok: every entry's HMAC matches `key` (None when no key was given).
        n: entries checked before the first failure. head=True with a key: when a head record exists for this log,
        also tail_ok (False when the log ends before the last entry the head recorded, or is missing or empty)."""
        prev = "0" * 64
        n = 0
        mac_ok = None if key is None else True
        seen: dict = {}
        res = None
        for line in (AuditLog.lines(path) if path.exists() else []):
            try:
                e = json.loads(line)
                h = e.pop("hash")
                m = e.pop("mac", None)
                body = json.dumps(e, ensure_ascii=False, sort_keys=True).encode("utf-8")
                linked = e["prev"] == prev and hashlib.sha256(body).hexdigest() == h
                mac_good = key is None or (isinstance(m, str) and
                                           hmac.compare_digest(m, hmac.new(key, body, hashlib.sha256).hexdigest()))
            except (ValueError, KeyError, TypeError, AttributeError):
                linked = mac_good = False
            if not linked:
                res = {"chain_ok": False, "mac_ok": False if key else None, "n": n}
                break
            if not mac_good:
                res = {"chain_ok": True, "mac_ok": False, "n": n}
                break
            seen[e.get("seq")] = h
            prev = h
            n += 1
        if res is None:
            res = {"chain_ok": True, "mac_ok": mac_ok, "n": n}
        if head and key is not None:
            hd = AuditLog._read_head(AuditLog.head_path(path), key, str(Path(path).resolve()))
            if hd is not None:
                res["tail_ok"] = bool(hd) and seen.get(hd["seq"]) == hd["hash"]
        return res

    @staticmethod
    def verify(path: Path, key: bytes | None = None) -> tuple[bool, int]:
        r = AuditLog.verify_detail(path, key)
        return r["chain_ok"] and r["mac_ok"] is not False, r["n"]


def cache_key(img_sha: str, model_id: str, raw: bool = False) -> str:
    """raw = OCR lines + the model's reply (depends on prompt and model only);
    final = after post-processing (also depends on READER_VERSION)."""
    if raw:
        h = hashlib.sha256((EXTRACT_PROMPT + model_id).encode("utf-8")).hexdigest()[:12]
        return f"{img_sha[:24]}_{h}_raw"
    h = hashlib.sha256((EXTRACT_PROMPT + READER_VERSION + TOTAL_ASK_KEY + model_id).encode("utf-8")).hexdigest()[:12]
    return f"{img_sha[:24]}_{h}"


def read_docs(docs: list[dict], reader=None, *, cache=True, progress=None, model_id: str | None = None) -> dict[str, dict]:
    cdir = OUT / "cache"
    cdir.mkdir(parents=True, exist_ok=True)
    out = {}
    for i, d in enumerate(docs):
        sha = sha256_file(d["path"])
        mid = reader.model_id if reader else (model_id or "Qwen3.5-2B-int4-ov")
        cp = cdir / f"{cache_key(sha, mid)}.json"
        rp = cdir / f"{cache_key(sha, mid, raw=True)}.json"
        if cache and cp.exists():
            res = json.loads(cp.read_text(encoding="utf-8"))
            res["cached"] = True
        else:
            if reader is None:
                raise RuntimeError(f"no cached read for {d['doc_id']} and no reader loaded")
            raw = json.loads(rp.read_text(encoding="utf-8")) if (cache and rp.exists()) else None
            raw_cached = raw is not None
            if raw is None:
                raw = reader.read_raw(d["path"])
                rp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            res = reader.read(d["path"], raw)
            if raw.pop("_dirty", False):   # the reader asked a follow-up question (the total): keep its answer
                rp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            res["sha256"] = sha
            res["cached"] = raw_cached
            cp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        out[d["doc_id"]] = res
        if progress:
            progress(i + 1, len(docs), d["doc_id"], res)
    return out


def crop_evidence(img_path: Path, box, out_path: Path, pad: int = 18) -> list[int] | None:
    if not box:
        return None
    im = Image.open(img_path)
    x0, y0, x1, y1 = box
    b = [max(0, x0 - pad), max(0, y0 - pad), min(im.width, x1 + pad), min(im.height, y1 + pad)]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.crop(b).save(out_path)
    return b


def audit(set_name: str, reader=None, *, run_id: str | None = None, cache=True, progress=None,
          registry=None, operator: str | None = None, model_id: str | None = None, read_fn=None,
          platform_invoices: set[str] | dict[str, set[str]] | None = None,
          platform_void: set[str] | dict[str, set[str]] | None = None, platform: str | None = None) -> dict:
    """registry: optional business-registration lookup (registry.py); the evaluation never passes one.
    read_fn: replaces read_docs (same signature), e.g. tiered.TieredReader.read_docs for the two-card mode.
    platform_invoices: invoice numbers already in the e-invoice platform download for this period (one set, or
    {client_id: set}); a photographed voucher with one of them is left out of the file (rules.run_rules). Default none.
    platform_void: numbers the platform marks 作廢, same shapes; a photographed voucher with one is held.
    platform="auto": load both from data/platform/<client UBN>/ for each client's own period (the web app does this;
    the evaluation does not, so its scores are unchanged). Explicit platform_invoices / platform_void win."""
    clients, docs = load_inbox(set_name)
    if platform == "auto":
        from . import einvoice_platform
        issued, void = einvoice_platform.platform_hooks_by_client(clients)
        platform_invoices = issued if platform_invoices is None else platform_invoices
        platform_void = void if platform_void is None else platform_void
    run_id = run_id or f"{set_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    rdir = OUT / "runs" / run_id
    log = AuditLog(rdir / "audit_log.jsonl")
    log.add("run_start", run_id=run_id, inbox=f"data/{set_name}", n_docs=len(docs), operator=operator or getpass.getuser(),
            host=_platform_mod.node(), code_rev=git_rev(), reader_version=READER_VERSION,
            model=reader.model_id if reader else f"cache ({model_id or 'Qwen3.5-2B-int4-ov'})", device=reader.device if reader else "cache",
            network="none: OCR and model run locally (OpenVINO); no outbound requests", basis=PII_RULE,
            registry=registry.label if registry is not None else None)
    t0 = time.time()
    reads = (read_fn or read_docs)(docs, reader, cache=cache, progress=progress, model_id=model_id)
    # a read_fn may mark a document it could not read at all ({"read_failed": {"reason", "detail"}}, e.g. the
    # reader process died on it twice): no rules run on it, it goes to a person as READ_REVIEW with that reason
    failed = {d["doc_id"]: reads[d["doc_id"]]["read_failed"] for d in docs if reads[d["doc_id"]].get("read_failed")}
    for d in docs:
        r = reads[d["doc_id"]]
        log.add("doc_read", doc_id=d["doc_id"], sha256=r.get("sha256") or sha256_file(d["path"]),
                model=r.get("model"), device=r.get("device"), cached=r.get("cached", False),
                seconds=r["timing"].get("total_s"), provenance=r.get("provenance"),
                **({"tier": r["tier"]} if "tier" in r else {}),
                **({"read_failed": failed[d["doc_id"]]} if d["doc_id"] in failed else {}))
    rdocs = [to_rule_doc(d, reads[d["doc_id"]]["fields"], reads[d["doc_id"]]["provenance"], reads[d["doc_id"]].get("ocr_lines"))
             for d in docs if d["doc_id"] not in failed]
    excs = rules.run_rules(rdocs, clients, registry=registry, platform_invoices=platform_invoices,
                           platform_void=platform_void)
    rdoc_by_id = {r.doc_id: r for r in rdocs}
    for d in docs:   # tiered mode: the second reader's process died before re-reading this document
        crash = (reads[d["doc_id"]].get("tier") or {}).get("second_reader_crashed")
        if crash:
            excs.append(rules.Exception_(d["doc_id"], d["client_id"], "READ_REVIEW", "review",
                                         "The second reader stopped before it could re-read this document "
                                         f"({crash}); check it by eye.", ["total"], confidence="low"))
        f = failed.get(d["doc_id"])
        if f:
            excs.append(rules.Exception_(d["doc_id"], d["client_id"], "READ_REVIEW", "review",
                                         f"This document was not read ({f.get('reason')}: {f.get('detail')}); "
                                         "check it by eye.", [], confidence="low"))
    by_id = {d["doc_id"]: d for d in docs}
    out_ex = []
    for k, e in enumerate(excs):
        ed = e.to_dict()
        if e.rule_id == "READ_REVIEW" and e.doc_id in failed:
            ed["reason"] = failed[e.doc_id].get("reason")
        r = reads[e.doc_id]
        fields = EVIDENCE.get(e.rule_id) or e.evidence_fields
        ev = []
        for f in fields:
            box = r["boxes"].get(f)
            if not box:
                continue
            slug = e.doc_id.replace("/", "_").replace(".jpg", "")
            cp = rdir / "crops" / f"{slug}_{f}.png"
            b = crop_evidence(by_id[e.doc_id]["path"], box, cp)
            ev.append({"field": f, "value": r["fields"].get(f) if f != "items" else
                       "; ".join(i["desc"] for i in r["fields"].get("items", [])),
                       "box": b, "crop": str(cp.relative_to(OUT)).replace("\\", "/"),
                       "provenance": r["provenance"].get(f)})
        ed["evidence"] = ev
        ed["image"] = f"{set_name}/clients/{e.doc_id}"
        ed["image_size"] = r.get("image_size")
        out_ex.append(ed)
        log.add("exception", doc_id=e.doc_id, rule_id=e.rule_id, severity=e.severity,
                citations=[c["url"] for c in ed["citations"] if c["url"]])
    elapsed = round(time.time() - t0, 2)
    per_doc_read = [reads[d["doc_id"]]["timing"].get("total_s") for d in docs]
    per_doc_read = [x for x in per_doc_read if x is not None]
    summary = {
        "run_id": run_id, "set": set_name, "n_docs": len(docs), "n_exceptions": len(out_ex),
        "by_rule": {rid: sum(1 for e in out_ex if e["rule_id"] == rid) for rid in rules.ISSUE_TYPES},
        "n_review": sum(1 for e in out_ex if e["rule_id"] == "READ_REVIEW"),
        "n_warning": sum(1 for e in out_ex if e["severity"] == "warning"),
        "by_client": {cid: sum(1 for e in out_ex if e["client_id"] == cid) for cid in clients},
        "elapsed_s": elapsed,
        "mean_read_s": round(sum(per_doc_read) / len(per_doc_read), 2) if per_doc_read else None,
        "unreadable_fields": sum(1 for d in docs for v in reads[d["doc_id"]]["provenance"].values() if v in UNCONFIRMED),
    }
    log.add("run_end", **{k: v for k, v in summary.items() if k != "run_id"})
    result = {
        "synthetic": True, "summary": summary,
        "clients": list(clients.values()),
        "docs": [{"doc_id": d["doc_id"], "client_id": d["client_id"], "month": d["month"],
                  "image": f"{set_name}/clients/{d['doc_id']}", "fields": reads[d["doc_id"]]["fields"],
                  "provenance": reads[d["doc_id"]]["provenance"], "boxes": reads[d["doc_id"]]["boxes"],
                  "image_size": reads[d["doc_id"]].get("image_size"),
                  "timing": reads[d["doc_id"]]["timing"],
                  # voucher kind and 免用統一發票 come from the OCR text, which result.json does not keep: the export
                  # needs them (format code 22 / 28, the period's small-business receipt total)
                  "tax_voucher_kind": rdoc_by_id[d["doc_id"]].tax_voucher_kind if d["doc_id"] in rdoc_by_id else None,
                  "small_business_receipt": rdoc_by_id[d["doc_id"]].small_business_receipt if d["doc_id"] in rdoc_by_id else False,
                  **({"tier": reads[d["doc_id"]]["tier"]} if "tier" in reads[d["doc_id"]] else {})} for d in docs],
        "exceptions": out_ex,
        "rules": rules.RULES,
        "audit_log": str((rdir / "audit_log.jsonl").relative_to(OUT)).replace("\\", "/"),
        "export_media": str((rdir / "export_media.csv").relative_to(OUT)).replace("\\", "/"),
    }
    # 401 input total must include small-business plain receipts (G10): per client, this period
    result["summary"]["small_business_receipts"] = export.period_summary(result)
    result["export_summary"] = str((rdir / "export_summary.csv").relative_to(OUT)).replace("\\", "/")
    export.write_csv(result, rdir / "export_media.csv")
    export.write_summary_csv(result, rdir / "export_summary.csv")
    (rdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="main")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--cache-only", action="store_true", help="do not load the model; use cached reads")
    args = ap.parse_args()
    reader = None
    if not args.cache_only:
        from .reader import Reader
        reader = Reader()
        print(f"model {reader.model_id} on {reader.device} (load {reader.load_s}s)")

    def prog(i, n, doc_id, res):
        t = res["timing"].get("total_s")
        print(f"[{i}/{n}] {doc_id} {'cached' if res.get('cached') else f'{t}s'}", flush=True)

    from .registry import load_default
    reg = load_default()
    if reg is not None:
        print(f"business register: {reg.label}")
    res = audit(args.set, reader, cache=not args.no_cache, progress=prog, registry=reg)
    print(json.dumps(res["summary"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
