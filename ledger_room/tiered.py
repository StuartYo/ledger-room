"""Tiered reading on two cards: a fast reader reads every document, a larger reader re-reads the unsure ones.

    first reader (9B, GPU.0)  -> reads every document, in order
                                 unsure? -> queue -> second reader (27B, GPU.1) re-reads it while the first goes on
    code                       -> merges the two readings field by field, then the usual rules decide

Both readers go through the same OCR lines and the same reconciliation code (reader.postprocess). The second reading
is one more vote. It never overwrites a field the first reading already confirmed on the page, and a field still
unconfirmed after both readings keeps its provenance, so the rule engine sends it to a person exactly as before.

"Unsure" is fixed here and written down in docs/tiered_draft.md before any tiered run. Do not tune it on results.
"""
from __future__ import annotations

import copy
import importlib
import json
import multiprocessing as mp
import queue
import threading
import time
from collections import Counter
from pathlib import Path

from PIL import Image

from . import pipeline, rules

ESCALATION_RULE_VERSION = "t1 (2026-11-06)"

# ---- when a document goes to the second reader (fixed; see module docstring) ---------------------------------------
# The fields the rule engine sends to a person (READ_REVIEW) when their provenance is unconfirmed.
ROUTED_FIELDS = ("seller_ubn", "buyer_ubn", "total")
UNSURE = frozenset(pipeline.UNCONFIRMED)          # {"model-only", "unconfirmed"}


def escalation_reasons(res: dict) -> list[str]:
    """Why the first reading is not good enough on its own. Empty list = keep it.

    reply_unparsed        the model's reply was not readable JSON
    <field>_unconfirmed   seller UBN, buyer UBN or total is model-only or unconfirmed (for the total: no two
                          agreeing sources, i.e. not two printed sources that match)
    amounts_inconsistent  net + tax != total, or tax is not 5% of net (the AMOUNT_MISMATCH hold)
    """
    reasons = []
    if not res.get("parsed", True):
        reasons.append("reply_unparsed")
    prov = res.get("provenance") or {}
    for f in ROUTED_FIELDS:
        if prov.get(f) in UNSURE:
            reasons.append(f"{f}_unconfirmed")
    f = res.get("fields") or {}
    if rules.amounts_inconsistent(f.get("net"), f.get("tax"), f.get("total")):
        reasons.append("amounts_inconsistent")
    return reasons


# ---- merging two readings ----------------------------------------------------------------------------------------
KEY_FIELDS = ("doc_type", "invoice_no", "date", "seller_ubn", "buyer_ubn", "total")
MODEL_FIELDS = ("seller_name", "buyer_name", "items", "note", "category", "invoice_period")
WEAK = UNSURE | {"missing"}


def _confirmed(prov: str | None, value) -> bool:
    """A field the reconciliation code confirmed from the page (OCR match, snap, zoom vote, two agreeing sources)."""
    return value is not None and prov is not None and prov not in WEAK and prov != "needs-zoom"


def merge(first: dict, second: dict, reasons: list[str]) -> dict:
    """First reading + second reading -> one reading. Code decides, field by field:

    - a field the first reading confirmed stays as it is (the second reading cannot overwrite it);
    - a field the first reading left weak (model-only, unconfirmed, missing) takes the second reading's value only
      when the reconciliation code confirmed that value from the page;
    - when the total comes from the second reading, net and tax come with it (code derived them from that total);
    - when net + tax still do not match the total and the second reading has the same total with net and tax that
      do match, its net and tax are used (the arithmetic is checked by code, not taken on trust);
    - when the first reply did not parse at all, the first reading has no model vote, so the second reading's
      model values fill the empty fields, keeping their own provenance (model-only stays model-only and is still
      routed to a person by the rules).
    Everything else stays from the first reading."""
    out = copy.deepcopy(first)
    fields, prov, boxes = out["fields"], out["provenance"], out.setdefault("boxes", {})
    f2, p2, b2 = second.get("fields") or {}, second.get("provenance") or {}, second.get("boxes") or {}
    unparsed = not first.get("parsed", True)
    second_parsed = second.get("parsed", True)
    taken: list[str] = []

    def take(k: str) -> None:
        fields[k] = copy.deepcopy(f2.get(k))
        if k in p2:
            prov[k] = p2[k]
        if k in b2:
            boxes[k] = b2[k]
        elif k in boxes:
            del boxes[k]
        taken.append(k)

    for k in KEY_FIELDS:
        if prov.get(k) not in WEAK:
            continue   # confirmed by the first reading, or a code decision (dropped, no buyer label): stays
        if _confirmed(p2.get(k), f2.get(k)) or (unparsed and second_parsed and f2.get(k) is not None
                                                 and fields.get(k) is None):
            take(k)
    if "total" in taken:
        for k in ("net", "tax"):
            take(k)
        out.setdefault("extra", {})["total_evidence_second"] = (second.get("extra") or {}).get("total_evidence")
    elif (rules.amounts_inconsistent(fields.get("net"), fields.get("tax"), fields.get("total"))
          and f2.get("total") == fields.get("total")
          and f2.get("net") is not None and f2.get("tax")
          and not rules.amounts_inconsistent(f2.get("net"), f2.get("tax"), fields.get("total"))):
        for k in ("net", "tax"):
            take(k)
    if unparsed and second_parsed:
        for k in MODEL_FIELDS:
            if not fields.get(k) and f2.get(k):
                take(k)
    out["tier"] = {"escalated": True, "reasons": list(reasons), "second_model": second.get("model"),
                   "second_device": second.get("device"), "second_parsed": second_parsed,
                   "taken_from_second": taken, "second_s": (second.get("timing") or {}).get("total_s")}
    t = out.setdefault("timing", {})
    t["first_s"] = t.get("total_s")
    t["second_s"] = (second.get("timing") or {}).get("total_s")
    t["total_s"] = round((t.get("first_s") or 0) + (t.get("second_s") or 0), 2)
    return out


# ---- the second read reuses the first read's OCR lines ------------------------------------------------------------
def read_raw_reusing_ocr(reader, path: Path, ocr_lines: list[dict]) -> dict:
    """Same as Reader.read_raw, but with the OCR lines already found by the first read: both readers see exactly
    the same lines (so the printed sources the code counts are the same), and OCR does not run twice."""
    from .reader import CATEGORIES, EXTRACT_PROMPT, Line, _fmt_ocr
    lines = [Line(l["id"], l["text"], tuple(l["box"]), l["conf"]) for l in ocr_lines]
    img = Image.open(path).convert("RGB")
    prompt = EXTRACT_PROMPT.format(ocr=_fmt_ocr(lines), cats=", ".join(CATEGORIES))
    raw, timing = reader.generate(prompt, img)
    timing["ocr_s"] = 0.0
    timing["ocr_reused"] = True
    return {"ocr_lines": ocr_lines, "raw": raw, "timing": timing, "image_size": list(img.size),
            "model": reader.model_id, "device": reader.device}


def read_with_ocr(reader, path: Path, ocr_lines: list[dict], raw: dict | None = None) -> tuple[dict, dict, bool]:
    """One second-reader pass (model call unless raw is given, then the reconciliation code).
    Returns (raw, reading, raw_changed)."""
    fresh = raw is None
    if fresh:
        raw = read_raw_reusing_ocr(reader, path, ocr_lines)
    res = reader.read(path, raw)
    dirty = bool(raw.pop("_dirty", False))
    return raw, res, fresh or dirty


# ---- the second reader in its own process -------------------------------------------------------------------------
class SecondReaderCrashed(RuntimeError):
    """The second reader's process died (for example a native abort inside the GPU plugin)."""


def _second_process_main(factory: str, model_dir: str, device: str, req_q, resp_q) -> None:
    """Child process: load one reader on one device, then answer requests until None arrives. Only this process
    touches that device. A Python error in one document is sent back; a native crash simply ends the process."""
    try:
        import ledger_room  # noqa: F401  (CI=true before openvino is imported in this process too)
        mod, fn = factory.split(":")
        t = time.time()
        reader = getattr(importlib.import_module(mod), fn)(model_dir, device)
        resp_q.put(("ready", {"model_id": reader.model_id, "device": reader.device,
                              "thinking": getattr(reader, "thinking", None), "load_s": round(time.time() - t, 1)}))
    except BaseException as ex:   # noqa: BLE001
        resp_q.put(("load_error", f"{type(ex).__name__}: {ex}"[:500]))
        return
    while True:
        req = req_q.get()
        if req is None:
            return
        rid, path, ocr_lines, raw = req
        try:
            resp_q.put((rid, "ok", read_with_ocr(reader, Path(path), ocr_lines, raw)))
        except Exception as ex:   # noqa: BLE001
            resp_q.put((rid, "error", f"{type(ex).__name__}: {ex}"[:300]))


class ProcessReader:
    """Stand-in for a Reader whose model lives in a separate process (multiprocessing, spawn), bound to its own
    device. On the B70 host, two VLMPipelines in one process used from two threads aborted the whole process inside
    the GPU plugin (ocl_stream.cpp, 2026-11-06). With the second reader in a child process, a crash there ends only
    the child: read_with_ocr then raises SecondReaderCrashed and the main process goes on."""

    def __init__(self, model_dir: str, device: str, *, factory: str = "ledger_room.reader:Reader",
                 load_timeout_s: float = 900.0, poll_s: float = 1.0):
        ctx = mp.get_context("spawn")
        self.req_q, self.resp_q = ctx.Queue(), ctx.Queue()
        self.poll_s = poll_s
        self.proc = ctx.Process(target=_second_process_main, name="tier2-reader",
                                args=(factory, str(model_dir), device, self.req_q, self.resp_q), daemon=True)
        self.proc.start()
        self.pid = self.proc.pid
        self.crashed: str | None = None
        self._rid = 0
        self._lock = threading.Lock()
        msg = self._wait(lambda m: m[0] in ("ready", "load_error"), load_timeout_s)
        if msg[0] == "load_error":
            self.close()
            raise RuntimeError(f"second reader failed to load: {msg[1]}")
        info = msg[1]
        self.model_id, self.device = info["model_id"], info["device"]
        self.thinking, self.load_s = info.get("thinking"), info.get("load_s")

    def _wait(self, want, timeout_s: float | None = None) -> tuple:
        t0 = time.time()
        while True:
            try:
                m = self.resp_q.get(timeout=self.poll_s)
            except queue.Empty:
                if not self.proc.is_alive():
                    try:   # a reply may have landed just before the process ended
                        m = self.resp_q.get(timeout=0.5)
                    except queue.Empty:
                        self.crashed = f"second reader process exited with code {self.proc.exitcode}"
                        raise SecondReaderCrashed(self.crashed) from None
                elif timeout_s is not None and time.time() - t0 > timeout_s:
                    raise TimeoutError(f"second reader did not answer in {timeout_s} s")
                else:
                    continue
            if want(m):
                return m

    def read_with_ocr(self, path: Path, ocr_lines: list[dict], raw: dict | None = None) -> tuple[dict, dict, bool]:
        with self._lock:
            if self.crashed:
                raise SecondReaderCrashed(self.crashed)
            self._rid += 1
            rid = self._rid
            self.req_q.put((rid, str(path), ocr_lines, raw))
            _, status, payload = self._wait(lambda m: m[0] == rid)
        if status != "ok":
            raise RuntimeError(payload)
        return payload

    def close(self) -> None:
        if self.proc.is_alive():
            try:
                self.req_q.put(None)
                self.proc.join(timeout=30)
            except Exception:   # noqa: BLE001
                pass
        if self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(timeout=10)


def _model_s(res: dict) -> float:
    t = res.get("timing") or {}
    return sum(t.get(k) or 0 for k in ("vlm_s", "ask_s", "zoom_s"))


class TieredReader:
    """Use with pipeline.audit(set, tr, read_fn=tr.read_docs). second=None runs the first reader alone (the
    "9B only" mode, or a tiered run whose second model is missing); unsure documents are then only counted."""

    def __init__(self, first, second=None, *, second_note: str | None = None, escalate=escalation_reasons):
        self.first, self.second = first, second
        self.second_note = second_note
        self.escalate = escalate
        self.model_id = first.model_id if second is None else f"{first.model_id}+{second.model_id}"
        self.device = first.device if second is None else f"{first.device}+{second.device}"
        self.stats: dict | None = None

    # pipeline.read_docs signature, so audit() can call it in its place
    def read_docs(self, docs: list[dict], reader=None, *, cache=True, progress=None, model_id=None) -> dict[str, dict]:
        t_start = time.perf_counter()
        out: dict[str, dict] = {}
        q: queue.Queue = queue.Queue()
        lock = threading.Lock()
        busy = {"first": 0.0, "second": 0.0}
        model_s = {"first": 0.0, "second": 0.0}
        reasons_n: Counter = Counter()
        taken_n: Counter = Counter()
        escalated: list[str] = []
        errors: list[dict] = []
        crashed: list[str] = []
        done = [0]
        t_marks = {}

        def finish(d, res):
            with lock:
                out[d["doc_id"]] = res
                done[0] += 1
                n = done[0]
            if progress:
                progress(n, len(docs), d["doc_id"], res)

        def second_worker():
            while True:
                item = q.get()
                if item is None:
                    return
                d, res, reasons = item
                t = time.perf_counter()
                try:
                    r2 = self._second_read(d, res, cache)
                    model_s["second"] += _model_s(r2)
                    merged = merge(res, r2, reasons)
                    taken_n.update(merged["tier"]["taken_from_second"])
                except SecondReaderCrashed as ex:   # the child process died: this document goes to a person
                    merged = copy.deepcopy(res)
                    merged["tier"] = {"escalated": True, "reasons": reasons, "second_model": self.second.model_id,
                                      "taken_from_second": [], "second_reader_crashed": str(ex)[:300]}
                    crashed.append(d["doc_id"])
                except Exception as ex:   # keep the first reading; its unsure fields still go to a person
                    merged = copy.deepcopy(res)
                    merged["tier"] = {"escalated": True, "reasons": reasons, "second_model": self.second.model_id,
                                      "taken_from_second": [], "second_error": f"{type(ex).__name__}: {ex}"[:300]}
                    errors.append({"doc_id": d["doc_id"], "error": merged["tier"]["second_error"]})
                busy["second"] += time.perf_counter() - t
                t_marks["second_last"] = time.perf_counter()
                finish(d, merged)

        th = None
        if self.second is not None:
            th = threading.Thread(target=second_worker, name="tier2-reader", daemon=True)
            th.start()
        try:
            for d in docs:
                t = time.perf_counter()
                res = pipeline.read_docs([d], self.first, cache=cache)[d["doc_id"]]
                busy["first"] += time.perf_counter() - t
                model_s["first"] += _model_s(res)
                reasons = self.escalate(res)
                reasons_n.update(reasons)
                if reasons and self.second is not None:
                    escalated.append(d["doc_id"])
                    q.put((d, res, reasons))
                    continue
                res["tier"] = {"escalated": False, "reasons": reasons,
                               **({"second": self.second_note or "no second reader"} if self.second is None else {})}
                finish(d, res)
            t_marks["first_done"] = time.perf_counter()
        finally:
            if th is not None:
                q.put(None)
                th.join()
        t_end = time.perf_counter()
        wall = t_end - t_start
        unsure = sum(1 for d in docs if out[d["doc_id"]]["tier"].get("reasons"))
        self.stats = {
            "first_model": self.first.model_id, "first_device": self.first.device,
            "second_model": self.second.model_id if self.second is not None else None,
            "second_device": self.second.device if self.second is not None else None,
            "second_note": self.second_note,
            "escalation_rule": ESCALATION_RULE_VERSION,
            "n_docs": len(docs), "unsure_docs": unsure, "escalated": len(escalated),
            "escalated_docs": escalated, "reasons": dict(reasons_n), "fields_taken_from_second": dict(taken_n),
            "second_errors": errors,
            "second_reader_crashed": len(crashed), "second_reader_crashed_docs": crashed,
            "second_reader_crash": getattr(self.second, "crashed", None),
            "read_wall_s": round(wall, 2),
            "first_done_s": round(t_marks.get("first_done", t_end) - t_start, 2),
            "second_tail_s": round(max(0.0, t_end - t_marks.get("first_done", t_end)), 2),
            "busy_s": {"first": round(busy["first"], 2), "second": round(busy["second"], 2)},
            "model_call_s": {"first": round(model_s["first"], 2), "second": round(model_s["second"], 2)},
            # > 0 means the two cards really worked at the same time (sum of busy time beyond the wall clock)
            "overlap_s": round(max(0.0, busy["first"] + busy["second"] - wall), 2),
        }
        return {d["doc_id"]: out[d["doc_id"]] for d in docs}

    def _second_read(self, d: dict, first_res: dict, cache: bool) -> dict:
        """The second reader on one document, with the first read's OCR lines. Cached next to the pipeline's own
        cache under a separate key (its timing has no OCR in it, so it must not pass for a normal read)."""
        r = self.second
        sha = first_res.get("sha256") or pipeline.sha256_file(d["path"])
        cdir = pipeline.OUT / "cache"
        cdir.mkdir(parents=True, exist_ok=True)
        cp = cdir / f"{pipeline.cache_key(sha, r.model_id)}_t2.json"
        rp = cdir / f"{pipeline.cache_key(sha, r.model_id, raw=True)}_t2.json"
        if cache and cp.exists():
            res = json.loads(cp.read_text(encoding="utf-8"))
            res["cached"] = True
            return res
        raw = json.loads(rp.read_text(encoding="utf-8")) if (cache and rp.exists()) else None
        raw_cached = raw is not None
        if hasattr(r, "read_with_ocr"):   # ProcessReader: the model runs in the child process
            raw, res, changed = r.read_with_ocr(d["path"], first_res["ocr_lines"], raw)
        else:
            raw, res, changed = read_with_ocr(r, d["path"], first_res["ocr_lines"], raw)
        if changed:
            rp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        res["sha256"] = sha
        res["cached"] = raw_cached
        cp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        return res
