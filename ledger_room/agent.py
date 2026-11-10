"""The review agent: a larger vision model (27B) with tools looks at the documents the rules held for a person, and
code checks what it proposes. "AI checks, code verifies, a person signs."

    held document (READ_REVIEW / AMOUNT_MISMATCH / BUYER_PRESUMED: a reading the rules do not trust)
      -> agent     27B, one tool call per turn, temperature 0 (greedy): zoom (crop, enlarge, OCR the crop),
                   calc (net + tax = total, tax = 5% of net), ubn_check (check digit), history (the firm's other
                   documents: confirmed seller UBNs, the same invoice number), platform (this client's e-invoice
                   platform download, when there is one), finish (proposed values + evidence, or "leave to a person")
      -> code      every proposed value is checked again here; the agent's word counts for nothing:
                   right format and check digit, and at least one source that is not a model (verify() below);
                   then the rules run again on the corrected document
      -> verified  no hold or review left: the document does not wait for a person ("agent-verified"); its original
                   reasons, every tool call and every check are kept in result.json and in the audit log
         held      anything else: the document goes to a person exactly as before; the agent's record stays beside it

Verification rule v3 (fixed 2026-11-09 after the experiment; docs/agent.md):
  invoice number, UBN, date: the code-run OCR of the agent's zoom crop reads the same string, or the page OCR has it
      and the first reader (9B) said the same; a UBN that OCR places with the other party is refused.
  amounts: net + tax = total and tax = 5% of net, AND the other two numbers were confirmed on the first reading or
      are printed on the page (OCR). "The number appears on the page" alone is NOT evidence for an amount: net, tax and
      total are all numbers on the page, and v1 accepted a net as the total that way.
  a receipt with no net and no tax: the total sits on an OCR line that carries a total label.
  amounts must add up after the fix; a UBN may not equal the other party's.
Do not loosen these on results.

Nothing here is sent anywhere: the model runs on this machine (OpenVINO).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from PIL import Image

from . import normalize as N
from . import rules
from . import ubn as ubnlib

AGENT_VERSION = "a1 (2026-11-09)"
VERIFY_VERSION = "v3"
DEFAULT_AGENT_MODEL = "Qwen3.5-27B-int4-ov"
KEY_FIELDS = ["doc_type", "invoice_no", "date", "seller_ubn", "buyer_ubn", "net", "tax", "total"]
FIXABLE = ["invoice_no", "date", "seller_ubn", "buyer_ubn", "net", "tax", "total"]
READING_RULES = {"READ_REVIEW", "AMOUNT_MISMATCH", "BUYER_PRESUMED"}
HELD_SEVERITY = ("hold", "review")
MAX_STEPS = 6
PROVENANCE = "agent-verified"
UNCONFIRMED = {"model-only", "unconfirmed"}           # pipeline.UNCONFIRMED (not imported: pipeline imports this)
WEAK = UNCONFIRMED | {"missing", "needs-zoom", None}


# ---------------------------------------------------------------- where the agent runs --------------------------------
def availability() -> dict:
    """Is there an agent model on this machine? Never loads anything.
    LEDGER_AGENT=0 turns the agent off. LEDGER_AGENT_MODEL_DIR names the model folder (default: the 27B next to the
    reader's model folder). LEDGER_AGENT_DEVICE puts it on another card (e.g. GPU.1); default: the reader's device."""
    from .reader import DEFAULT_MODEL_DIR
    md = os.environ.get("LEDGER_AGENT_MODEL_DIR") or str(Path(DEFAULT_MODEL_DIR).parent / DEFAULT_AGENT_MODEL)
    dev = os.environ.get("LEDGER_AGENT_DEVICE") or os.environ.get("LEDGER_DEVICE") or None
    out = {"model": Path(md).name, "model_dir": md, "device": dev}
    if os.environ.get("LEDGER_AGENT", "1") == "0":
        return out | {"available": False, "reason": "off"}
    try:
        ok = (Path(md) / "openvino_language_model.xml").exists() or any(Path(md).glob("*.xml"))
    except OSError:
        ok = False
    return out | ({"available": True, "reason": None} if ok else {"available": False, "reason": "no_model"})


def card_memory_gb() -> dict:
    """Card memory this process holds (Linux DRM fdinfo, as eval/bench_one_card.py measures it); {} elsewhere."""
    tot, seen = {}, set()
    fd = Path(f"/proc/{os.getpid()}/fdinfo")
    if not fd.exists():
        return {}
    for f in fd.glob("*"):
        try:
            txt = f.read_text()
        except Exception:
            continue
        if "drm-driver" not in txt:
            continue
        cid = re.search(r"drm-client-id:\s*(\d+)", txt)
        if cid and cid.group(1) in seen:
            continue
        if cid:
            seen.add(cid.group(1))
        for m in re.finditer(r"^(drm-(?:total|resident)-(?:vram\d*|gtt|system\d*|memory)):\s*(\d+)\s*(KiB|MiB|B)?",
                             txt, flags=re.M):
            mult = {"KiB": 1 / 2**20, "MiB": 1 / 2**10, "B": 1 / 2**30, None: 1 / 2**20}[m.group(3)]
            k = "gpu_" + m.group(1).replace("drm-", "").replace("-", "_") + "_gb"
            tot[k] = tot.get(k, 0.0) + int(m.group(2)) * mult
    return {k: round(v, 2) for k, v in tot.items()}


class Agent:
    """The agent model (a reader.Reader: generate(prompt, image, max_new_tokens) with do_sample off = temperature 0).
    model: any object with that generate() and model_id / device (tests pass a scripted fake)."""

    def __init__(self, model=None, model_dir: str | None = None, device: str | None = None):
        if model is None:
            from .reader import Reader
            av = availability()
            model = Reader(model_dir or av["model_dir"], device=device or av["device"])
        self.model = model
        self.model_id = getattr(model, "model_id", "agent")
        self.device = getattr(model, "device", None)
        self.load_s = getattr(model, "load_s", None)


# ---------------------------------------------------------------- value helpers ---------------------------------------
def digits_tokens(text: str) -> set[str]:
    return {t.replace(",", "") for t in re.findall(r"\d[\d,]*\d|\d", text or "")}


def inv_tokens(text: str) -> set[str]:
    return {(a + b).upper() for a, b in re.findall(r"([A-Za-z]{2})\s*[-－]?\s*(\d{8})(?!\d)", text or "")}


def date_tokens(text: str) -> set[str]:
    out = set()
    for m in N._DATE.finditer(text or ""):
        d = N.parse_date(m.group(0))
        if d:
            out.add(d.isoformat())
    return out


def norm_value(field: str, v):
    if v is None or v == "":
        return None
    if field in ("seller_ubn", "buyer_ubn"):
        if isinstance(v, int):          # JSON number: the model's leading zero was lost in the JSON, not on the page
            return str(v).zfill(8)
        s = re.sub(r"\D", "", str(v))
        return s or None
    if field == "invoice_no":
        return re.sub(r"[\s\-－]", "", str(v)).upper() or None
    if field == "date":
        d = N.parse_date(v)
        return d.isoformat() if d else str(v)
    if field in ("net", "tax", "total"):
        return N.to_int(v)
    return v


def text_has(field: str, value, text: str) -> bool:
    if value is None:
        return False
    if field in ("seller_ubn", "buyer_ubn", "net", "tax", "total"):
        return str(value) in digits_tokens(text)
    if field == "invoice_no":
        return value in inv_tokens(text) or (len(value) == 10 and value[2:] in digits_tokens(text)
                                             and value[:2] in re.sub(r"[^A-Z]", " ", text.upper()).split())
    if field == "date":
        return value in date_tokens(text)
    return False


def format_ok(field: str, v) -> bool:
    if v is None:
        return False
    if field in ("seller_ubn", "buyer_ubn"):
        return len(v) == 8 and v.isdigit() and ubnlib.is_valid_new(v)
    if field == "invoice_no":
        return bool(re.fullmatch(r"[A-Z]{2}\d{8}", v))
    if field == "date":
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v))
    if field in ("net", "tax", "total"):
        return isinstance(v, int) and v >= 0
    return False


def model_obj_value(field: str, obj: dict):
    key = {"invoice_no": "no", "date": "date", "seller_ubn": "seller_ubn", "buyer_ubn": "buyer_ubn",
           "net": "net", "tax": "tax", "total": "total"}[field]
    return norm_value(field, obj.get(key))


# ---------------------------------------------------------------- tools -----------------------------------------------
class Tools:
    """What the agent may call. Every result that can serve as evidence is kept (crop OCR text, confirmed seller UBNs
    from other documents, platform records), so code can check the proposals, now or later from the stored record.
    ocr: read_lines replacement (tests); platform_records: this client's platform input records, or None to load
    them from data/platform when asked."""

    def __init__(self, doc, read, clients, all_reads, all_docs, ocr=None, platform_records=None):
        self.doc, self.read, self.clients = doc, read, clients
        self.all_reads, self.all_docs = all_reads, all_docs
        self.img = Image.open(doc["path"]).convert("RGB")
        self._ocr = ocr
        self._platform = platform_records
        self.crop_texts: list[str] = []
        self.history_hits: dict[str, set] = {"seller_ubn": set()}
        self.platform_hits: list[dict] = []

    def evidence(self) -> dict:
        return {"crop_texts": list(self.crop_texts), "history_seller_ubn": sorted(self.history_hits["seller_ubn"]),
                "platform_hits": list(self.platform_hits)}

    def zoom(self, box):
        import numpy as np
        if self._ocr is None:
            from .ocr import read_lines
            self._ocr = read_lines
        W, H = self.img.size
        try:
            x0, y0, x1, y1 = [int(float(v)) for v in box]
        except Exception:
            return "zoom needs box [x0,y0,x1,y1] in page pixels", None
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        pw, ph = max(20, (x1 - x0) // 5), max(20, (y1 - y0) // 3)
        b = (max(0, x0 - pw), max(0, y0 - ph), min(W, x1 + pw), min(H, y1 + ph))
        if b[2] - b[0] < 8 or b[3] - b[1] < 8:
            return "box is outside the page or too small", None
        crop = self.img.crop(b)
        s = min(3.0, 896 / max(crop.size))
        if s > 1:
            crop = crop.resize((round(crop.width * s), round(crop.height * s)), Image.LANCZOS)
        if crop.height < 448:      # thin strips crash the GPU plugin now and then (reader.ZOOM_MIN_H)
            canvas = Image.new("RGB", (crop.width, 448), "white")
            canvas.paste(crop, (0, (448 - crop.height) // 2))
            crop = canvas
        lines = self._ocr(np.asarray(crop))
        text = " | ".join(l.text for l in lines)
        self.crop_texts.append(text)
        return f"zoomed {list(b)}; code OCR of the enlarged crop: {text[:600] or '(nothing read)'}", crop

    @staticmethod
    def calc(net=None, tax=None, total=None):
        net, tax, total = N.to_int(net), N.to_int(tax), N.to_int(total)
        parts = []
        if net is not None and tax is not None:
            parts.append(f"net+tax={net + tax}")
            parts.append(f"5% of net={round(net * 0.05)}")
        if total is not None:
            parts.append(f"total/1.05={round(total / 1.05)}, tax inside total={total - round(total / 1.05)}")
        parts.append("consistent" if not rules.amounts_inconsistent(net, tax, total) and None not in (net, tax, total)
                     else ("NOT consistent" if None not in (net, tax, total) else "incomplete"))
        return "; ".join(parts)

    @staticmethod
    def ubn_check(ubn=None):
        c = ubnlib.check(re.sub(r"\D", "", str(ubn or "")))
        if not c.well_formed:
            return "not 8 digits"
        return f"{c.ubn}: check digit {'valid' if c.valid_new else 'INVALID'} (new rule)"

    def history(self, seller_name=None, invoice_no=None):
        out = []
        if seller_name:
            seen = {}
            for d in self.all_docs:
                if d["doc_id"] == self.doc["doc_id"]:
                    continue
                r = self.all_reads[d["doc_id"]]
                f, p = r.get("fields") or {}, r.get("provenance") or {}
                if f.get("seller_ubn") and p.get("seller_ubn") not in WEAK and \
                        N.name_sim(f.get("seller_name"), seller_name) >= 0.85:
                    seen.setdefault(f["seller_ubn"], []).append(d["doc_id"])
            for u, ids in seen.items():
                self.history_hits["seller_ubn"].add(u)
                out.append(f"seller UBN {u} confirmed on {len(ids)} other document(s) of this firm")
            if not seen:
                out.append("no other confirmed document from a seller with that name")
        if invoice_no:
            inv = norm_value("invoice_no", invoice_no)
            same = [d["doc_id"] for d in self.all_docs if d["doc_id"] != self.doc["doc_id"]
                    and (self.all_reads[d["doc_id"]].get("fields") or {}).get("invoice_no") == inv]
            out.append(f"invoice {inv} also on: {same}" if same else f"invoice {inv} not on any other document")
        return "; ".join(out) or "give seller_name or invoice_no"

    def platform(self, invoice_no=None):
        recs = self._platform
        if recs is None:
            from . import einvoice_platform as P
            c = self.clients[self.doc["client_id"]]
            try:
                recs = P.load_client(c, c.get("period"))["input"]
            except Exception as e:
                return f"platform download not available ({type(e).__name__})"
        if not recs:
            return "no platform download for this client"
        inv = norm_value("invoice_no", invoice_no)
        hit = [r for r in recs if r.get("invoice_no") == inv]
        if not hit:
            return f"invoice {inv} not in the platform download ({len(recs)} input records)"
        r = hit[0]
        self.platform_hits.append({k: r.get(k) for k in ("invoice_no", "seller_ubn", "amount", "tax")})
        return f"platform: invoice {inv} seller UBN {r.get('seller_ubn')} net {r.get('amount')} tax {r.get('tax')}"


# ---------------------------------------------------------------- the agent loop --------------------------------------
PROMPT = """You are a review agent at a Taiwanese bookkeeping firm. The checking program held this document for a person.
Why it was held:
{reasons}
Values the program has now (value, and where it came from: ocr-confirmed / snapped / ocr = found on the page by OCR; model-only = only the vision model read it; unconfirmed = sources disagree; missing = not found):
{fields}
The first vision model's own reply: {model}
OCR text lines of the page (id | text | box x0,y0,x1,y1). Page size {w} x {h} pixels:
{ocr}

Your job: for each held field, either propose the value printed on the document, backed by evidence you gathered with the tools, or leave it to a person. Never guess. If a value is covered, stamped over, faded or unclear, leave it to a person: a wrong value that slips into a tax filing is far worse than one more document for a person. The program re-checks every value you propose against the page; unsupported values go to a person anyway.
Tools (exactly one per reply):
{{"tool":"zoom","box":[x0,y0,x1,y1]}}   enlarge a region of the page (page pixels); the program also runs OCR on the enlarged crop
{{"tool":"calc","net":N,"tax":N,"total":N}}   arithmetic check (net + tax = total, tax = 5% of net)
{{"tool":"ubn_check","ubn":"12345678"}}   check digit of a 統一編號
{{"tool":"history","seller_name":"...","invoice_no":"..."}}   seller UBNs confirmed on this firm's other documents; same invoice number elsewhere
{{"tool":"platform","invoice_no":"AB12345678"}}   this client's e-invoice platform download
{{"tool":"finish","fixes":{{"field":"value"}},"evidence":{{"field":"what you saw"}},"need_human":["field"],"note":"one sentence for the bookkeeper"}}
Fields you may fix: {fixable}. Put a field in fixes even when you only confirm the current value.
Steps so far:
{steps}
{image_note}
Reply with ONE line of JSON only."""


def fmt_fields(f, p):
    return "\n".join(f"  {k} = {f.get(k)!r} ({p.get(k) or 'missing'})" for k in KEY_FIELDS)


def fmt_ocr(lines, limit=90):
    return "\n".join(f"  {l['id']} | {l['text']} | {','.join(str(x) for x in l['box'])}" for l in lines[:limit])


def held(excs, doc_id=None):
    """Exceptions that send a document to a person (severity hold or review), as rules.Exception_ or dicts."""
    def g(e, k):
        return e.get(k) if isinstance(e, dict) else getattr(e, k)
    return [e for e in excs if g(e, "severity") in HELD_SEVERITY and (doc_id is None or g(e, "doc_id") == doc_id)]


def fixable_fields(exceptions) -> list[str]:
    fx = {f for e in exceptions if e.rule_id in READING_RULES for f in e.evidence_fields if f in FIXABLE}
    if fx & {"net", "tax", "total"}:      # an amount is checked as a family (net + tax = total)
        fx |= {"net", "tax", "total"}
    return sorted(fx)


def run_agent(model, tools: Tools, read, exceptions) -> dict:
    """The tool loop for one document. Returns {"steps", "final", "fixable", "seconds", "model_s"}."""
    reasons = "\n".join(f"  [{e.rule_id}] {e.message} (fields: {', '.join(e.evidence_fields)})" for e in exceptions)
    fixable = fixable_fields(exceptions)
    if not fixable:
        return {"steps": [], "final": {"fixes": {}, "need_human": [], "note": "no reading field to fix"},
                "fixable": [], "seconds": 0.0, "model_s": 0.0}
    w, h = tools.img.size
    steps, trace = [], []
    final, bad = None, 0
    image = tools.img
    t0 = time.time()
    model_s = 0.0
    for i in range(MAX_STEPS + 1):
        force = i >= MAX_STEPS
        note = ("The image shows the enlarged crop from your last zoom." if image is not tools.img
                else "The image shows the whole page.")
        if force:
            note += " You have used all your steps: reply with finish now."
        prompt = PROMPT.format(reasons=reasons, fields=fmt_fields(read["fields"], read["provenance"]),
                               model=(read.get("raw") or "")[:900], w=w, h=h, ocr=fmt_ocr(read.get("ocr_lines") or []),
                               fixable=", ".join(fixable), steps="\n".join(steps) or "  (none yet)", image_note=note)
        reply, tm = model.generate(prompt, image, max_new_tokens=320)
        model_s += tm.get("vlm_s", 0) or 0
        obj = N.parse_json_loose(reply) or {}
        tool = obj.get("tool")
        rec = {"step": i + 1, "reply": reply[:700], "tool": tool, "vlm_s": tm.get("vlm_s")}
        if tool == "finish" or force:
            final = obj if tool == "finish" else {"fixes": {}, "need_human": fixable, "note": "no finish given"}
            trace.append(rec)
            break
        if tool == "zoom":
            res, crop = tools.zoom(obj.get("box") or [])
            if crop is not None:
                image = crop
        elif tool == "calc":
            res = tools.calc(obj.get("net"), obj.get("tax"), obj.get("total"))
        elif tool == "ubn_check":
            res = tools.ubn_check(obj.get("ubn"))
        elif tool == "history":
            res = tools.history(obj.get("seller_name"), obj.get("invoice_no"))
        elif tool == "platform":
            res = tools.platform(obj.get("invoice_no"))
        else:
            bad += 1
            res = "reply was not one tool call in JSON; reply with one line of JSON"
            if bad >= 2:
                final = {"fixes": {}, "need_human": fixable, "note": "agent reply unreadable"}
                trace.append(rec)
                break
        rec["args"] = {k: v for k, v in obj.items() if k != "tool"}
        rec["result"] = res[:700]
        trace.append(rec)
        args = {k: v for k, v in obj.items() if k != "tool"}
        steps.append(f"  {i + 1}. {json.dumps({'tool': tool, **args}, ensure_ascii=False)[:200]} -> {res[:500]}")
    return {"steps": trace, "final": final or {}, "fixable": fixable, "seconds": round(time.time() - t0, 2),
            "model_s": round(model_s, 2)}


# ---------------------------------------------------------------- code check (v3) -------------------------------------
def verify(agent: dict, read: dict, evidence: dict) -> dict:
    """Which proposed values code accepts, and why (or why not). evidence: Tools.evidence() of that run.
    Returns {"fields": {field: {"value", "accepted", "evidence", "why"}}, "accepted": {field: value}}."""
    f, p = read.get("fields") or {}, read.get("provenance") or {}
    ocr_lines = read.get("ocr_lines") or []
    page_text = " ".join(l["text"] for l in ocr_lines)
    crop_texts = list(evidence.get("crop_texts") or [])
    history_ubn = set(evidence.get("history_seller_ubn") or [])
    platform_hits = list(evidence.get("platform_hits") or [])
    model = N.parse_json_loose(read.get("raw") or "") or {}
    fixes = (agent.get("final") or {}).get("fixes") or {}
    if not isinstance(fixes, dict):
        fixes = {}
    props = {k: norm_value(k, v) for k, v in fixes.items() if k in agent.get("fixable", [])}
    out = {}
    new = dict(f)
    new.update({k: v for k, v in props.items() if v is not None})
    for k, v in props.items():
        ev = []
        if not format_ok(k, v):
            out[k] = {"value": v, "accepted": False, "evidence": [], "why": "format"}
            continue
        amount = k in ("net", "tax", "total")
        # an amount is never "evidenced" by its digits appearing somewhere on the page (v1 accepted a net as the total)
        if not amount and any(text_has(k, v, t) for t in crop_texts):
            ev.append("crop-ocr")
        if not amount and text_has(k, v, page_text) and model_obj_value(k, model) == v:
            ev.append("page-ocr+first-reader")
        if amount:
            all_text = [page_text] + crop_texts

            def seen(x):
                return new.get(x) is not None and ((p.get(x) not in WEAK and new.get(x) == f.get(x))
                                                   or any(text_has(x, new.get(x), t) for t in all_text))
            if new.get("net") is None and new.get("tax") is None:
                # no tax shown (receipt-style): the total must sit on an OCR line that carries a total label
                if k == "total" and any(re.search(r"總計|合計|總額|總金額|應付|實付|Total|TOTAL", ln)
                                        and str(v) in digits_tokens(ln)
                                        for ln in [l["text"] for l in ocr_lines]
                                        + [s for t in crop_texts for s in t.split(" | ")]):
                    ev.append("total-label-line")
            elif all(seen(x) for x in ("net", "tax", "total") if x != k) and new.get("tax") and \
                    not rules.amounts_inconsistent(new.get("net"), new.get("tax"), new.get("total")):
                # v3: net + tax = total and tax = 5% of net, and the OTHER two numbers are confirmed or printed on the
                # page; the fixed one may be unreadable (it follows from the other two)
                ev.append("arithmetic")
        if k in ("seller_ubn", "buyer_ubn"):
            from .ocr import Line
            from .reader import ubn_roles
            other = "buyer" if k == "seller_ubn" else "seller"
            ln = [Line(x["id"], x["text"], tuple(x["box"]), x["conf"]) for x in ocr_lines]
            roles = ubn_roles(ln)
            mine = {t for t, _ in roles[k.split("_")[0]]}
            if v in {t for t, _ in roles[other]} and v not in mine:
                out[k] = {"value": v, "accepted": False, "evidence": [], "why": f"ocr-other-party:{other}"}
                continue
        if k == "seller_ubn" and v in history_ubn:
            ev.append("history")
        for r in platform_hits:
            if (k == "seller_ubn" and r.get("seller_ubn") == v) or (k == "net" and r.get("amount") == v) or \
                    (k == "tax" and r.get("tax") == v) or (k == "invoice_no" and r.get("invoice_no") == v):
                ev.append("platform")
        if k == "buyer_ubn" and v == new.get("seller_ubn") or k == "seller_ubn" and v == new.get("buyer_ubn"):
            out[k] = {"value": v, "accepted": False, "evidence": [], "why": "same-as-other-party"}
            continue
        out[k] = {"value": v, "accepted": bool(ev), "evidence": ev, "why": "ok" if ev else "no-independent-source"}
    # amounts must add up after the fix
    acc = {k: o["value"] for k, o in out.items() if o["accepted"]}
    trial = dict(f)
    trial.update(acc)
    if any(k in acc for k in ("net", "tax", "total")) and rules.amounts_inconsistent(
            trial.get("net"), trial.get("tax"), trial.get("total")):
        for k in ("net", "tax", "total"):
            if k in acc:
                out[k]["accepted"] = False
                out[k]["why"] = "amounts-do-not-add-up"
        acc = {k: v for k, v in acc.items() if k not in ("net", "tax", "total")}
    return {"fields": out, "accepted": acc}


# ---------------------------------------------------------------- one batch -------------------------------------------
def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def trace_key(img_sha: str, model_id: str, read: dict, exceptions) -> str:
    """A stored agent run is reused only for the same image, agent model, prompt, step limit, first reading and
    reasons (the run itself depends on nothing else: greedy decoding)."""
    body = json.dumps({"img": img_sha, "model": model_id, "v": AGENT_VERSION, "prompt": _sha(PROMPT), "steps": MAX_STEPS,
                       "fields": read.get("fields"), "prov": read.get("provenance"), "raw": read.get("raw"),
                       "ocr": read.get("ocr_lines"),
                       "reasons": [[e.rule_id, e.message, list(e.evidence_fields)] for e in exceptions]},
                      ensure_ascii=False, sort_keys=True, default=str)
    return _sha(body)[:40]


def _rule_doc(d, f, p, read):
    from .pipeline import to_rule_doc
    return to_rule_doc(d, f, p, read.get("ocr_lines"), read.get("raw"))


def process(docs: list[dict], reads: dict, rdocs: dict, excs: list, clients: dict, agent: Agent | None = None, *,
            cache_dir: Path | None = None, use_cache: bool = True, rules_kw: dict | None = None,
            all_held: bool = False, ocr=None, progress=None) -> dict[str, dict]:
    """Run the agent on every held document with a reading reason, check its proposals, and say for each whether the
    document is cleared. Documents are judged one at a time against the first reading of all the others.
    agent None: only stored runs (cache_dir) are used ("replayed"); documents without one get no record.
    all_held: also documents held for other reasons only (the evaluation counts them; the agent has nothing to fix).
    Returns {doc_id: record}; record["accepted"] are the values to apply when record["status"] == "verified"."""
    rules_kw = rules_kw or {}
    by_id = {d["doc_id"]: d for d in docs}
    held_ids = sorted({e.doc_id for e in held(excs) if e.doc_id in rdocs})
    todo = [i for i in held_ids if all_held or fixable_fields(held(excs, i))]
    out = {}
    for n, doc_id in enumerate(todo):
        # progress(done, total, doc_id, None) before each document it looks at, and (total, total, None, None) at the end
        if progress:
            progress(n, len(todo), doc_id, None)
        ex = held(excs, doc_id)
        d, r = by_id[doc_id], reads[doc_id]
        img_sha = r.get("sha256") or hashlib.sha256(Path(d["path"]).read_bytes()).hexdigest()
        stored = None
        cp = None
        mid = agent.model_id if agent is not None else None
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
            if mid:
                cp = cache_dir / f"{img_sha[:16]}_{trace_key(img_sha, mid, r, ex)}.json"
                if use_cache and cp.exists():
                    stored = json.loads(cp.read_text(encoding="utf-8"))
            else:       # replay: a stored run of this document with this first reading and these reasons
                for p in sorted(cache_dir.glob(f"{img_sha[:16]}_*.json")):
                    try:
                        s = json.loads(p.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if s.get("key") == trace_key(img_sha, s.get("model"), r, ex):
                        stored = s
                        break
        replayed = stored is not None and agent is None
        if stored is None:
            if agent is None:
                continue
            try:
                tools = Tools(d, r, clients, reads, docs, ocr=ocr)
                ag = run_agent(agent.model, tools, r, ex)
                stored = {"key": trace_key(img_sha, mid, r, ex), "model": mid, "device": agent.device,
                          "agent_version": AGENT_VERSION, "run": ag, "evidence": tools.evidence(),
                          "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            except Exception as e:      # the agent failing never stops the check: the document stays with a person
                out[doc_id] = {"status": "error", "error": f"{type(e).__name__}: {e}"[:300], "model": mid,
                               "device": agent.device, "held_for": _reasons(ex), "accepted": {}, "steps": []}
                continue
            if cp is not None:
                tmp = cp.with_name(cp.name + ".tmp")
                tmp.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
                tmp.replace(cp)
        ag = stored["run"]
        v = verify(ag, r, stored.get("evidence") or {})
        newf, newp = dict(r.get("fields") or {}), dict(r.get("provenance") or {})
        newf.update(v["accepted"])
        newp.update({k: PROVENANCE for k in v["accepted"]})
        nd = _rule_doc(d, newf, newp, r)
        ex2 = held(rules.run_rules([nd if x.doc_id == doc_id else x for x in rdocs.values()], clients, **rules_kw),
                   doc_id)
        final = ag.get("final") or {}
        out[doc_id] = {
            "status": "verified" if not ex2 and v["accepted"] else "held",
            "model": stored.get("model"), "device": stored.get("device"), "replayed": replayed,
            "agent_version": stored.get("agent_version"), "verify_version": VERIFY_VERSION,
            "seconds": ag.get("seconds"), "model_s": ag.get("model_s"),
            "held_for": _reasons(ex), "fixable": ag.get("fixable", []),
            "steps": [{k: s.get(k) for k in ("step", "tool", "args", "result", "reply", "vlm_s") if k in s}
                      for s in ag.get("steps", [])],
            "proposed": {k: norm_value(k, x) for k, x in (final.get("fixes") or {}).items()
                         if isinstance(final.get("fixes"), dict)},
            "need_human": final.get("need_human") if isinstance(final.get("need_human"), list) else [],
            "note": str(final.get("note") or "")[:300],
            "checks": {k: {**o, "old": (r.get("fields") or {}).get(k)} for k, o in v["fields"].items()},
            "accepted": v["accepted"],
            "machine": {k: (r.get("fields") or {}).get(k) for k in v["accepted"]},
            "machine_prov": {k: (r.get("provenance") or {}).get(k) for k in v["accepted"]},
            "still_held_for": [f"{e.rule_id}:{','.join(e.evidence_fields)}" for e in ex2],
        }
    if progress and todo:
        progress(len(todo), len(todo), None, None)
    return out


def _reasons(ex) -> list[dict]:
    return [{"rule_id": e.rule_id, "severity": e.severity, "message": e.message,
             "evidence_fields": list(e.evidence_fields)} for e in ex]


# ---------------------------------------------------------------- a person overrides the agent ------------------------
def overrides_state(entries) -> dict[str, dict]:
    """{doc_id: {"user", "ts", "note"}} for documents whose agent fixes a person rejected (review-log entries
    {"event": "agent_override", "action": "reject" | "restore"}), oldest first; a later restore cancels a reject."""
    out: dict[str, dict] = {}
    for e in entries:
        if e.get("event") != "agent_override" or not e.get("doc_id"):
            continue
        if e.get("action") == "reject":
            out[e["doc_id"]] = {k: e.get(k) for k in ("user", "ts", "note")}
        elif e.get("action") == "restore":
            out.pop(e["doc_id"], None)
    return out


def apply_overrides(result: dict, rejected: dict[str, dict]) -> dict:
    """A copy of result where each rejected document is back to its first reading: the agent's values are taken out
    (fields and provenance as first read) and the original reasons are back in the exceptions, so it waits for a
    person again. The agent's record stays, marked "overridden"."""
    import copy
    docs = {d["doc_id"]: d for d in result.get("docs", [])}
    hit = {k: v for k, v in (rejected or {}).items() if (docs.get(k) or {}).get("agent", {}).get("status") == "verified"}
    if not hit:
        return result
    out = copy.deepcopy(result)
    for d in out["docs"]:
        o = hit.get(d["doc_id"])
        if not o:
            continue
        a = d["agent"]
        for k, v in (a.get("machine") or {}).items():
            d["fields"][k] = v
            d.setdefault("provenance", {})[k] = (a.get("machine_prov") or {}).get(k)
        a["overridden"] = o
        keep = {(e.get("rule_id"), tuple(e.get("evidence_fields") or ())) for e in out["exceptions"]
                if e.get("doc_id") == d["doc_id"]}
        for e in a.get("held_exceptions") or []:
            if (e.get("rule_id"), tuple(e.get("evidence_fields") or ())) not in keep:
                out["exceptions"].append({**e, "restored": True})
    s = out.get("summary")
    if isinstance(s, dict):
        s["n_exceptions"] = len(out["exceptions"])
        s["n_agent_overridden"] = len(hit)
    return out


def log_entry(doc_id: str, rec: dict) -> dict:
    """The audit-log entry for one agent run: which tools it called, what each returned (shortened), what it proposed,
    which checks code ran on each value and the outcome."""
    return {"doc_id": doc_id, "status": rec.get("status"), "model": rec.get("model"), "device": rec.get("device"),
            "replayed": rec.get("replayed", False), "agent_version": rec.get("agent_version"),
            "verify_version": rec.get("verify_version"), "seconds": rec.get("seconds"),
            "held_for": [f"{e['rule_id']}:{','.join(e['evidence_fields'])}" for e in rec.get("held_for", [])],
            "tools": [{"tool": s.get("tool"), "args": s.get("args"), "result": (s.get("result") or "")[:300]}
                      for s in rec.get("steps", [])],
            "proposed": rec.get("proposed"), "need_human": rec.get("need_human"),
            "checks": {k: {"value": o.get("value"), "accepted": o.get("accepted"), "evidence": o.get("evidence"),
                           "why": o.get("why")} for k, o in (rec.get("checks") or {}).items()},
            "accepted": rec.get("accepted"), "still_held_for": rec.get("still_held_for"),
            **({"error": rec["error"]} if rec.get("error") else {})}
