"""Several readers per card: K independent reader processes on one (or two) B70 cards -> eval/results_concurrency.json

  .venv/bin/python -m eval.bench_concurrency --set heldout --k 1,2,3,4 --devices GPU.0 --models-root ~/models
  .venv/bin/python -m eval.bench_concurrency --set heldout --k 2,4,6,8 --devices GPU.0,GPU.1 --models-root ~/models --out results_concurrency_2cards.json
  python -m eval.bench_concurrency --dry-run          # laptop plumbing check: 2B on CPU, 3 documents, K = 1,2

Per K (one row each):
  K worker processes (multiprocessing spawn), each with its own reader (VLMPipeline) on its own device, round robin
  over --devices. A supervisor in the main process hands documents from one shared queue to whichever worker is
  idle. Each worker reads a document exactly like the normal pipeline (pipeline.read_docs, cache off) and sends the
  result back over its own pipe; the main process never loads a model.
  Recorded: docs/hour over the read wall clock (all workers loaded -> last document back; restarts after a crash are
  inside it, the first loads are not), per-document latency (seconds a worker spent on the document: mean, p50,
  p95, max), peak device memory, power and Wh per 1,000 documents (xpu-smi, below), and the usual scoring
  (pipeline.audit on the collected reads, then eval.read_accuracy + eval.run_eval.score as in bench_tiered), which
  must match K = 1 (same_scoring_as_k1, docs_fields_differ_vs_k1).

Supervisor (replaces bench_tiered --first-restart-every N): the GPU driver can abort a process outright
(CL_OUT_OF_RESOURCES, not a Python exception). A worker that dies (non-zero exit, signal, or no answer within
--doc-timeout-s) is logged with the document it was on, restarted (fresh process, model loaded again), and that
document is retried once. If it dies on the same document again, the document is not read: pipeline.audit gets
{"read_failed": {"reason": "reader_crash"}} for it and sends it to a person as READ_REVIEW (exception field
reason = "reader_crash"). A Python exception while reading counts the same way (reason "reader_error"), and the
worker process ends after reporting it, so the retry runs in a fresh process (on the B70 the GPU plugin is unusable
after such an exception). If no worker is left at all, the rest go to a person with reason "no_reader". No document is ever dropped.
Healthy runs pay nothing: there are no precautionary reloads.

Power and memory come from Intel's xpu-smi (see XpuSampler: the exact commands and field names are listed there and
recorded in the results file). xpu-smi device ids are assumed to be GPU.N -> N; override with --xpu-smi-ids
GPU.0=1,GPU.1=0 after checking `xpu-smi discovery` (its output is stored in the results file). Without xpu-smi
(laptop) power and xpu-smi memory are null with a note; on Linux the workers' own DRM fdinfo memory is recorded too.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import multiprocessing as mp
import os
import platform
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from multiprocessing.connection import wait as mp_wait
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_FACTORY = "ledger_room.reader:Reader"


# =================================================================================================================
# worker process
# =================================================================================================================
def _worker_main(factory: str, model_dir: str, device: str, out_dir: str, conn) -> None:
    """Child: load one reader on one device, then read the documents the supervisor sends until None arrives.
    A Python error in one document is sent back; a native abort simply ends the process (the supervisor sees it)."""
    try:
        import ledger_room  # noqa: F401  (CI=true before openvino is imported in this process too)
        from ledger_room import pipeline
        pipeline.OUT = Path(out_dir)   # same isolation as the main process (spawn starts from a fresh module)
        mod, fn = factory.split(":")
        t = time.perf_counter()
        reader = getattr(importlib.import_module(mod), fn)(model_dir, device)
        conn.send(("ready", {"model_id": reader.model_id, "device": reader.device, "pid": os.getpid(),
                             "thinking": getattr(reader, "thinking", None),
                             "load_s": round(time.perf_counter() - t, 2)}))
    except BaseException as ex:   # noqa: BLE001
        conn.send(("load_error", f"{type(ex).__name__}: {ex}"[:500]))
        return
    while True:
        try:
            d = conn.recv()
        except EOFError:
            return
        if d is None:
            return
        d = {**d, "path": Path(d["path"])}
        t = time.perf_counter()
        try:
            res = pipeline.read_docs([d], reader, cache=False)[d["doc_id"]]
            conn.send(("done", d["doc_id"], res, round(time.perf_counter() - t, 3)))
        except Exception as ex:   # noqa: BLE001
            conn.send(("error", d["doc_id"], f"{type(ex).__name__}: {ex}"[:300], round(time.perf_counter() - t, 3)))
            # on the B70 an inference exception leaves the GPU plugin unusable for this process ("any subsequent
            # OpenCL API call may cause the application to hang"); end the process so the supervisor starts a fresh
            # one, and the document is retried there
            return


def _exit_text(code) -> dict:
    out = {"exitcode": code}
    if isinstance(code, int) and code < 0:
        try:
            out["signal"] = signal.Signals(-code).name
        except ValueError:
            out["signal"] = str(-code)
    return out


class _Slot:
    def __init__(self, idx: int, device: str):
        self.idx, self.device = idx, device
        self.proc = self.conn = None
        self.state = "new"      # loading | idle | busy | dead
        self.doc: dict | None = None
        self.t_state = 0.0
        self.info: dict = {}
        self.load_crashes = 0
        self.pending_restart: dict | None = None
        self.docs_done = 0


class Supervisor:
    """K reader processes fed from one queue; restarts a dead worker and retries its document once."""

    def __init__(self, factory: str, model_dir: str, devices: list[str], out_dir: str, *, doc_timeout_s: float = 600,
                 load_timeout_s: float = 900, max_load_crashes: int = 2, max_restarts: int = 50, poll_s: float = 0.2,
                 log=print):
        self.factory, self.model_dir, self.out_dir = factory, str(model_dir), str(out_dir)
        self.slots = [_Slot(i, dev) for i, dev in enumerate(devices)]
        self.doc_timeout_s, self.load_timeout_s = doc_timeout_s, load_timeout_s
        self.max_load_crashes, self.max_restarts = max_load_crashes, max_restarts
        self.poll_s, self.log = poll_s, log
        self.ctx = mp.get_context("spawn")
        self.t0 = time.perf_counter()
        self.crashes: list[dict] = []
        self.restarts: list[dict] = []
        self.load_errors: list[dict] = []
        self._reset_run()

    def _reset_run(self):
        self.reads: dict[str, dict] = {}
        self.failed: dict[str, dict] = {}
        self.latency: dict[str, float] = {}
        self.attempts: dict[str, int] = {}
        self.pending: deque = deque()
        self.retries: list[dict] = []

    def _t(self) -> float:
        return round(time.perf_counter() - self.t0, 2)

    # ---- process handling ----
    def _spawn(self, s: _Slot) -> None:
        parent, child = self.ctx.Pipe(duplex=True)
        s.proc = self.ctx.Process(target=_worker_main, name=f"reader-{s.idx}",
                                  args=(self.factory, self.model_dir, s.device, self.out_dir, child), daemon=True)
        s.proc.start()
        child.close()
        s.conn, s.state, s.doc, s.t_state = parent, "loading", None, time.perf_counter()

    def _live(self) -> list[_Slot]:
        return [s for s in self.slots if s.state in ("loading", "idle", "busy", "exiting")]

    def start(self) -> list[dict]:
        """Load the workers one after another (no load-time memory spike from K loads at once)."""
        for s in self.slots:
            self._spawn(s)
            while s.state == "loading":
                self._step()
            self.log(f"  worker {s.idx} on {s.device}: {s.state} {s.info.get('load_s', '')}")
        return [{"worker": s.idx, "device": s.device, "state": s.state, **s.info} for s in self.slots]

    def _restart(self, s: _Slot, why: str) -> None:
        if len(self.restarts) >= self.max_restarts:
            s.state = "dead"
            self.log(f"  [supervisor] worker {s.idx}: restart limit ({self.max_restarts}) reached, not restarted")
            return
        rec = {"worker": s.idx, "device": s.device, "reason": why, "t_s": self._t(), "load_s": None}
        self.restarts.append(rec)
        s.pending_restart = rec
        self._spawn(s)

    # ---- the document queue ----
    def run(self, docs: list[dict]) -> dict:
        self._reset_run()
        self.pending.extend(docs)
        crashes0, restarts0 = len(self.crashes), len(self.restarts)
        t_run = time.perf_counter()
        while True:
            for s in self.slots:
                if s.state == "idle" and self.pending:
                    d = self.pending.popleft()
                    try:
                        s.conn.send({**d, "path": str(d["path"])})
                        s.state, s.doc, s.t_state = "busy", d, time.perf_counter()
                    except (OSError, EOFError, BrokenPipeError):   # died while idle: _step sees it
                        self.pending.appendleft(d)
            busy = [s for s in self.slots if s.state == "busy"]
            if not self.pending and not busy:
                break
            if not self._live():
                for d in list(self.pending):
                    self._fail_final(d, "no_reader", "every reader process had stopped and could not be restarted")
                self.pending.clear()
                break
            self._step()
        wall = time.perf_counter() - t_run
        return {"reads": self.reads, "failed": self.failed, "latency_s": self.latency, "wall_s": wall,
                "crashes": self.crashes[crashes0:], "restarts": self.restarts[restarts0:], "retries": self.retries,
                "docs_per_worker": {s.idx: s.docs_done for s in self.slots}}

    def _step(self) -> None:
        live = self._live()
        if not live:
            return
        mp_wait([s.conn for s in live] + [s.proc.sentinel for s in live], timeout=self.poll_s)
        now = time.perf_counter()
        for s in live:
            self._drain(s)
            if s.state not in ("loading", "idle", "busy", "exiting"):
                continue
            if s.state == "exiting" and s.proc.is_alive() and now - s.t_state > 30:
                s.proc.kill()   # it should have ended by itself right after reporting the error
                s.proc.join(timeout=10)
            if not s.proc.is_alive():
                self._drain(s)   # an answer may have landed just before the end
                self._died(s, timeout=False)
            elif s.state == "busy" and now - s.t_state > self.doc_timeout_s:
                s.proc.kill()
                s.proc.join(timeout=10)
                self._died(s, timeout=True)
            elif s.state == "loading" and now - s.t_state > self.load_timeout_s:
                s.proc.kill()
                s.proc.join(timeout=10)
                self._died(s, timeout=True)

    def _drain(self, s: _Slot) -> None:
        while s.conn is not None:
            try:
                if not s.conn.poll():
                    return
                msg = s.conn.recv()
            except (EOFError, OSError):
                return
            self._handle(s, msg)

    def _handle(self, s: _Slot, msg: tuple) -> None:
        kind = msg[0]
        if kind == "ready":
            s.info, s.state = msg[1], "idle"
            if s.pending_restart is not None:
                s.pending_restart["load_s"] = msg[1].get("load_s")
                s.pending_restart = None
                self.log(f"  [supervisor] worker {s.idx} back on {s.device} (load {msg[1].get('load_s')} s)")
        elif kind == "load_error":
            s.state = "dead"
            s.pending_restart = None
            self.load_errors.append({"worker": s.idx, "device": s.device, "error": msg[1], "t_s": self._t()})
            self.log(f"  [supervisor] worker {s.idx} on {s.device} could not load: {msg[1]}")
        elif kind == "done":
            _, doc_id, res, secs = msg
            self.reads[doc_id] = res
            self.latency[doc_id] = secs
            s.docs_done += 1
            s.state, s.doc = "idle", None
        elif kind == "error":
            _, doc_id, err, secs = msg
            d = s.doc
            s.state, s.doc, s.t_state = "exiting", None, time.perf_counter()   # worker ends itself; restarted in _died
            if d is not None:
                self._fail(d, "reader_error", err, worker=s.idx)

    def _died(self, s: _Slot, *, timeout: bool) -> None:
        code = s.proc.exitcode
        rec = {"worker": s.idx, "device": s.device, "pid": s.info.get("pid") or s.proc.pid, **_exit_text(code),
               "timeout": timeout, "state": s.state, "doc_id": s.doc["doc_id"] if s.doc else None, "t_s": self._t()}
        try:
            s.conn.close()
        except OSError:
            pass
        was, d = s.state, s.doc
        s.doc = None
        if was == "exiting":   # expected: the worker reported a reader error and ended; not a crash
            self.log(f"  [supervisor] worker {s.idx} ({s.device}) ended after a reader error; starting a fresh one")
            self._restart(s, "after reader_error")
            return
        if was == "loading":
            s.load_crashes += 1
            rec["during"] = "load"
            self.crashes.append(rec)
            self.log(f"  [crash] worker {s.idx} ({s.device}) died while loading: {_exit_text(code)}")
            if s.load_crashes > self.max_load_crashes:
                s.state = "dead"
                s.pending_restart = None
                return
            self._restart(s, "died while loading")
            return
        rec["attempt"] = self.attempts.get(d["doc_id"], 0) + 1 if d else None
        self.crashes.append(rec)
        what = f"timeout after {self.doc_timeout_s} s" if timeout else str(_exit_text(code))
        self.log(f"  [crash] worker {s.idx} ({s.device}, pid {rec['pid']}) {what} on doc "
                 f"{d['doc_id'] if d else '(idle)'}")
        if d is not None:
            self._fail(d, "reader_crash", ("timeout" if timeout else f"exit {code}") +
                       (f" {rec['signal']}" if rec.get("signal") else ""), worker=s.idx)
        self._restart(s, f"crash on {d['doc_id']}" if d else "crash while idle")

    def _fail(self, d: dict, reason: str, detail: str, worker: int) -> None:
        n = self.attempts[d["doc_id"]] = self.attempts.get(d["doc_id"], 0) + 1
        if n < 2:
            self.retries.append({"doc_id": d["doc_id"], "reason": reason, "detail": detail, "worker": worker})
            self.log(f"  [retry] {d['doc_id']} once more ({reason}: {detail})")
            self.pending.appendleft(d)
        else:
            self._fail_final(d, reason, f"failed twice; last: {detail}")

    def _fail_final(self, d: dict, reason: str, detail: str) -> None:
        self.failed[d["doc_id"]] = {"reason": reason, "detail": detail, "attempts": self.attempts.get(d["doc_id"], 0)}
        self.log(f"  [to a person] {d['doc_id']} READ_REVIEW ({reason}: {detail})")

    def close(self) -> None:
        for s in self.slots:
            if s.proc is None:
                continue
            if s.proc.is_alive():
                try:
                    s.conn.send(None)
                except (OSError, EOFError, BrokenPipeError):
                    pass
                s.proc.join(timeout=30)
            if s.proc.is_alive():
                s.proc.terminate()
                s.proc.join(timeout=10)
            try:
                s.conn.close()
            except OSError:
                pass
            s.state = "closed"


# =================================================================================================================
# xpu-smi: power (W) and device memory
# =================================================================================================================
# What this relies on (Intel xpu-smi as installed on the B70 host, checked there 2026-11-07; the raw head of each
# stream is also stored in the results file under power.raw_head):
#
#   xpu-smi dump --device <id> --metrics 1,18 --interval 1 --number 1000000
#     (long flags only: this build rejects -m / -i / -n; `stdbuf -oL` in front when stdbuf exists so rows are not
#      held in a pipe buffer; the process is terminated when the K run ends)
#     metric 1 = power, metric 18 = memory used. Output: a whitespace-aligned table (older builds: CSV), e.g.
#       Timestamp      DeviceId    power.draw (W)    memory.used (MiB)
#       18:03:53...           0             76.83         1547.6328125
#     (the timestamp is truncated with "..."; it is not used, samples are stamped on arrival)
#     Columns are found by header name, never by position: power = a header holding "power" and "(W)"
#     ("power.draw (W)", old "GPU Power (W)"); memory = "memory.used" / "Memory Used" (unit from the parentheses:
#     MiB / GiB / MB / KiB / B; MiB if none). Header cells are split on commas or on 2+ spaces, data rows on commas
#     or any whitespace. "N/A" or an empty cell -> no value for that sample.
#
#   xpu-smi stats -d <id> -j    (JSON) read at the start and end of each K run for the energy counter, and polled
#                               every interval if dump gives no row:
#     {"device_index": 0, "pci_bdf": "0000:17:00.0", "timestamps": {...},
#      "power": {"gpu_power_w": {"tile_0": {"avg", "min", "max", "current"}}, "energy_consumed_j": 13.03}, ...}
#     power_w   = sum over tiles of "current" (else "avg"); a plain number is taken as is
#     energy_j  = power.energy_consumed_j. Used only if it behaves as a running counter over the K run (end > start
#                 and the implied mean power agrees with the sampled one within 0.5x..2x, or lies in 1..1000 W when
#                 nothing was sampled); otherwise Wh comes from the sampled mean W (recorded as energy_source).
#     memory    not seen yet on the host: any numeric key holding "used" inside a "memory" object (unit from the key:
#               _mib / _gib / _kib / _bytes / _b, else MiB, else bytes if > 1e7). Older JSON with
#               device_level[] {"metrics_type": "XPUM_STATS_POWER" / "XPUM_STATS_MEMORY_USED"} and the text table
#               ("| GPU Power (W) | 41 |") are still read.
#
#   xpu-smi discovery   stored raw (first 4000 chars). On the host: device 0 = PCI 0000:17:00.0, device 1 =
#                       0000:ba:00.0, both "Intel(R) Arc(TM) Pro B70 Graphics". GPU.N -> N is assumed; compare
#                       inventory.devices[].pci (OpenVINO) and override with --xpu-smi-ids if they differ.
_UNIT_GB = {"b": 1 / 2**30, "bytes": 1 / 2**30, "kib": 1 / 2**20, "kb": 1e3 / 2**30, "mib": 1 / 2**10,
            "mb": 1e6 / 2**30, "gib": 1.0, "gb": 1e9 / 2**30}


def _num(s) -> float | None:
    if s is None or isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"-?\d+(?:\.\d+)?", str(s))
    if not m or "n/a" in str(s).lower():
        return None
    return float(m.group(0))


def _mem_unit(header: str) -> float:
    m = re.search(r"\(([A-Za-z]+)\)", header)
    return _UNIT_GB.get(m.group(1).lower(), 1 / 2**10) if m else 1 / 2**10


_MEM_RE = re.compile(r"memory[ ._]used", re.I)


def dump_columns(header_line: str) -> dict | None:
    """Column positions in an xpu-smi dump header, by name. None if the line is not a header."""
    sep = r"\s*,\s*" if "," in header_line else r"\s{2,}"
    cols = [c.strip() for c in re.split(sep, header_line.strip()) if c.strip()]
    if not any(c.lower().startswith("timestamp") for c in cols):
        return None
    out = {"names": cols}
    for i, c in enumerate(cols):
        lc = c.lower()
        if "power" in lc and "(w)" in lc and "power" not in out:
            out["power"] = i
        elif _MEM_RE.search(c) and "memory" not in out:
            out["memory"], out["memory_to_gb"] = i, _mem_unit(c)
        elif lc.startswith("timestamp"):
            out["timestamp"] = i
    return out


def parse_dump_row(cols: dict, line: str) -> dict | None:
    cells = [c.strip() for c in (line.split(",") if "," in line else line.split())]
    if len(cells) < 2 or not re.match(r"^\d", cells[0]):
        return None
    p = _num(cells[cols["power"]]) if "power" in cols and cols["power"] < len(cells) else None
    m = _num(cells[cols["memory"]]) if "memory" in cols and cols["memory"] < len(cells) else None
    return {"power_w": p, "mem_gb": round(m * cols["memory_to_gb"], 3) if m is not None else None}


def parse_dump(text: str) -> list[dict]:
    """All sample rows of an xpu-smi dump output (header first)."""
    cols, rows = None, []
    for line in text.splitlines():
        if cols is None:
            cols = dump_columns(line)
            continue
        r = parse_dump_row(cols, line)
        if r is not None:
            rows.append(r)
    return rows


def _tile_power(v) -> float | None:
    if isinstance(v, dict):
        if any(k in v for k in ("current", "avg")):
            return _num(v.get("current", v.get("avg")))
        tiles = [_tile_power(x) for x in v.values()]
        tiles = [t for t in tiles if t is not None]
        return round(sum(tiles), 3) if tiles else None
    return _num(v)


def _mem_from_json(obj) -> float | None:
    """GB from the first numeric key holding 'used' inside a 'memory' object (searched at any depth)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "memory" in str(k).lower() and isinstance(v, dict):
                for kk, vv in v.items():
                    lk = str(kk).lower()
                    val = vv.get("current", vv.get("avg")) if isinstance(vv, dict) else vv
                    val = None if isinstance(val, (dict, list)) else _num(val)
                    if "used" not in lk or "util" in lk or val is None:
                        continue
                    unit = next((u for u in ("mib", "gib", "kib", "bytes", "mb", "gb", "kb") if lk.endswith(u)),
                                "b" if lk.endswith("_b") else None)
                    if unit is None:
                        unit = "bytes" if val > 1e7 else "mib"
                    return round(val * _UNIT_GB[unit], 3)
            r = _mem_from_json(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _mem_from_json(v)
            if r is not None:
                return r
    return None


def parse_stats(text: str) -> dict:
    """One sample from `xpu-smi stats -d <id> -j` (JSON) or its text table. Missing fields -> None."""
    out = {"power_w": None, "mem_gb": None, "energy_j": None}
    try:
        js = json.loads(text)
    except ValueError:
        js = None
    if isinstance(js, dict):
        pw = js.get("power")
        if isinstance(pw, dict):
            out["power_w"] = _tile_power(pw.get("gpu_power_w"))
            e = pw.get("energy_consumed_j")
            out["energy_j"] = None if isinstance(e, (dict, list)) else _num(e)
        for it in js.get("device_level") or []:   # older xpu-smi JSON
            t = str(it.get("metrics_type", "")).upper()
            v = _num(it.get("value", it.get("avg")))
            if v is None:
                continue
            if t == "XPUM_STATS_POWER" and out["power_w"] is None:
                out["power_w"] = v
            elif t == "XPUM_STATS_MEMORY_USED" and out["mem_gb"] is None:
                out["mem_gb"] = round(v / 2**30 if v > 1e7 else v / 2**10, 3)
            elif t == "XPUM_STATS_ENERGY" and out["energy_j"] is None:
                out["energy_j"] = v
        if out["mem_gb"] is None:
            out["mem_gb"] = _mem_from_json(js)
        return out
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        key = cells[0].lower()
        if "power" in key and "(w)" in key and out["power_w"] is None:
            out["power_w"] = _num(cells[1])
        elif _MEM_RE.search(key) and out["mem_gb"] is None:
            v = _num(cells[1])
            out["mem_gb"] = round(v * _mem_unit(cells[0]), 3) if v is not None else None
    return out


def energy_from_counter(start_j: float | None, end_j: float | None, wall_s: float,
                        sampled_mean_w: float | None) -> tuple[float | None, str]:
    """Joules over the run from a running energy counter, or (None, why not)."""
    if start_j is None or end_j is None:
        return None, "energy counter not available"
    d = end_j - start_j
    if d <= 0 or wall_s <= 0:
        return None, f"energy counter did not increase ({start_j} -> {end_j} J): not a running counter or it reset"
    implied = d / wall_s
    if sampled_mean_w:
        if not 0.5 <= implied / sampled_mean_w <= 2.0:
            return None, (f"energy counter implies {implied:.1f} W but samples average {sampled_mean_w:.1f} W: "
                          "not used")
    elif not 1.0 <= implied <= 1000.0:
        return None, f"energy counter implies {implied:.1f} W: not plausible, not used"
    return d, "ok"


class XpuSampler:
    """Background sampling of power and memory per device, about once a second (one thread per device)."""

    def __init__(self, smi_ids: dict[str, str], *, interval_s: float = 1.0, cmd: list[str] | None = None,
                 metrics: str = "1,18"):
        self.smi_ids, self.interval_s, self.metrics = dict(smi_ids), interval_s, metrics
        self.cmd = list(cmd) if cmd else ["xpu-smi"]
        self.samples: dict[str, list[tuple[float, float | None, float | None]]] = {d: [] for d in smi_ids}
        self.raw_head: dict[str, list[str]] = {d: [] for d in smi_ids}
        self.mode: dict[str, str] = {}
        self.notes: list[str] = []
        self._stop = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def available(self) -> bool:
        return shutil.which(self.cmd[0]) is not None or Path(self.cmd[0]).exists()

    def dump_cmd(self, smi_id: str) -> list[str]:
        pre = ["stdbuf", "-oL"] if self.cmd == ["xpu-smi"] and shutil.which("stdbuf") else []
        return pre + self.cmd + ["dump", "--device", str(smi_id), "--metrics", self.metrics,
                                 "--interval", str(max(1, round(self.interval_s))), "--number", "1000000"]

    def stats_cmd(self, smi_id: str) -> list[str]:
        return self.cmd + ["stats", "-d", str(smi_id), "-j"]

    def start(self) -> "XpuSampler":
        if not self.available():
            self.notes.append(f"{self.cmd[0]} not found: power and xpu-smi memory not measured")
            return self
        for dev, sid in self.smi_ids.items():
            th = threading.Thread(target=self._run_device, args=(dev, sid), daemon=True, name=f"xpu-smi-{dev}")
            th.start()
            self._threads.append(th)
        return self

    def _add(self, dev: str, power, mem) -> None:
        with self._lock:
            self.samples[dev].append((time.time(), power, mem))

    def _head(self, dev: str, line: str) -> None:
        if len(self.raw_head[dev]) < 4:
            self.raw_head[dev].append(line.rstrip()[:300])

    def _run_device(self, dev: str, sid: str) -> None:
        got = 0
        try:
            p = subprocess.Popen(self.dump_cmd(sid), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 bufsize=1)
            self._procs.append(p)
            self.mode[dev] = "dump: " + " ".join(self.dump_cmd(sid))
            cols = None
            for line in p.stdout:
                if self._stop.is_set():
                    break
                self._head(dev, line)
                if cols is None:
                    cols = dump_columns(line)
                    continue
                r = parse_dump_row(cols, line)
                if r is not None:
                    got += 1
                    self._add(dev, r["power_w"], r["mem_gb"])
        except OSError as ex:
            self.notes.append(f"{dev}: xpu-smi dump could not start: {ex}")
        if got or self._stop.is_set():
            return
        self.notes.append(f"{dev}: xpu-smi dump gave no sample rows; polling `xpu-smi stats -j` instead")
        self.mode[dev] = "stats: " + " ".join(self.stats_cmd(sid))
        while not self._stop.is_set():
            t = time.time()
            try:
                txt = subprocess.run(self.stats_cmd(sid), capture_output=True, text=True, timeout=10).stdout
                self._head(dev, txt[:300])
                r = parse_stats(txt)
                if r["power_w"] is not None or r["mem_gb"] is not None:
                    self._add(dev, r["power_w"], r["mem_gb"])
            except (OSError, subprocess.SubprocessError) as ex:
                self.notes.append(f"{dev}: xpu-smi stats failed: {ex}"[:200])
                return
            self._stop.wait(max(0.0, self.interval_s - (time.time() - t)))

    def stop(self) -> None:
        self._stop.set()
        for p in self._procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:   # noqa: BLE001
                try:
                    p.kill()
                    p.wait(timeout=5)
                except Exception:   # noqa: BLE001
                    pass
        for th in self._threads:
            th.join(timeout=12)

    def read_energy(self) -> dict[str, dict]:
        """power.energy_consumed_j of every device now (one `xpu-smi stats -d <id> -j` each, in parallel)."""
        out: dict[str, dict] = {}

        def one(dev, sid):
            try:
                txt = subprocess.run(self.stats_cmd(sid), capture_output=True, text=True, timeout=30).stdout
                out[dev] = {"energy_j": parse_stats(txt)["energy_j"], "t": time.time()}
            except (OSError, subprocess.SubprocessError) as ex:
                out[dev] = {"energy_j": None, "t": time.time(), "error": str(ex)[:200]}
        if not self.available():
            return {}
        ths = [threading.Thread(target=one, args=(d, s), daemon=True) for d, s in self.smi_ids.items()]
        for th in ths:
            th.start()
        for th in ths:
            th.join(timeout=40)
        return out

    def window(self, t0: float, t1: float) -> dict:
        """Per device, samples with t0 <= time <= t1 (time.time() clock)."""
        with self._lock:
            return {d: [s for s in v if t0 <= s[0] <= t1] for d, v in self.samples.items()}


def power_summary(samples: list[tuple]) -> dict:
    p = [s[1] for s in samples if s[1] is not None]
    return {"mean_w": round(statistics.mean(p), 2) if p else None, "max_w": round(max(p), 2) if p else None,
            "samples": len(p)}


def mem_peak_gb(samples: list[tuple]) -> float | None:
    m = [s[2] for s in samples if s[2] is not None]
    return round(max(m), 2) if m else None


def mem_mean_gb(samples: list[tuple]) -> float | None:
    m = [s[2] for s in samples if s[2] is not None]
    return round(statistics.mean(m), 2) if m else None


def energy(mean_w_total: float | None, wall_s: float, n_docs: int) -> dict:
    """Energy over the read wall clock = mean power x time. Wh per 1,000 documents = that / n_docs x 1000."""
    if mean_w_total is None or wall_s <= 0 or n_docs <= 0:
        return {"energy_wh": None, "wh_per_1000_docs": None}
    wh = mean_w_total * wall_s / 3600
    return {"energy_wh": round(wh, 3), "wh_per_1000_docs": round(wh / n_docs * 1000, 2)}


def combine_energy(e0: dict, e1: dict, sampled_mean_w: dict, mean_w_total: float | None, wall_s: float,
                   n_docs: int) -> dict:
    """Wh per 1,000 documents: from the energy counter delta when it is usable on every card in use, else from the
    sampled mean power x read wall clock. Both are kept."""
    sampled = energy(mean_w_total, wall_s, n_docs)
    per_dev, joules, ok = {}, 0.0, bool(sampled_mean_w)
    for d in sampled_mean_w:
        a, b = (e0.get(d) or {}), (e1.get(d) or {})
        span = (b.get("t") or 0) - (a.get("t") or 0)
        dj, why = energy_from_counter(a.get("energy_j"), b.get("energy_j"), span, (sampled_mean_w.get(d) or {}).get("mean_w"))
        per_dev[d] = {"start_j": a.get("energy_j"), "end_j": b.get("energy_j"), "span_s": round(span, 1),
                      "delta_j": round(dj, 1) if dj is not None else None, "status": why}
        ok = ok and dj is not None
        joules += dj or 0.0
    out = {"energy_counter": per_dev, "sampled_energy_wh": sampled["energy_wh"],
           "sampled_wh_per_1000_docs": sampled["wh_per_1000_docs"]}
    if ok and n_docs > 0:
        wh = joules / 3600
        out.update(energy_source="xpu-smi energy counter (power.energy_consumed_j, end - start)",
                   energy_wh=round(wh, 3), wh_per_1000_docs=round(wh / n_docs * 1000, 2))
    else:
        out.update(energy_source="sampled mean W x read wall clock" if sampled["energy_wh"] is not None else None,
                   **sampled)
    return out


# ---- per-process GPU memory from DRM fdinfo (Linux; xe / i915) --------------------------------------------------
def fdinfo_vram_gb(pid: int) -> float | None:
    """Sum of drm-total-vram* over the DRM clients of one process (KiB in fdinfo). None when not available."""
    base = Path(f"/proc/{pid}/fdinfo")
    if not base.exists():
        return None
    tot, seen, found = 0.0, set(), False
    for f in base.glob("*"):
        try:
            txt = f.read_text()
        except OSError:
            continue
        if "drm-driver" not in txt:
            continue
        cid = re.search(r"drm-client-id:\s*(\d+)", txt)
        if cid:
            if cid.group(1) in seen:
                continue
            seen.add(cid.group(1))
        for m in re.finditer(r"^drm-total-vram\d*:\s*(\d+)\s*(KiB|MiB|B)?", txt, flags=re.M):
            found = True
            tot += int(m.group(1)) * {"KiB": 1 / 2**20, "MiB": 1 / 2**10, "B": 1 / 2**30, None: 1 / 2**20}[m.group(2)]
    return round(tot, 3) if found else None


class FdinfoSampler:
    def __init__(self, sup: Supervisor, interval_s: float = 1.0):
        self.sup, self.interval_s = sup, interval_s
        self.peak_total: float | None = None
        self.peak_by_worker: dict[int, float] = {}
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "FdinfoSampler":
        if platform.system() == "Linux":
            self._t.start()
        return self

    def _run(self):
        while not self._stop.wait(self.interval_s):
            tot, any_ = 0.0, False
            for s in list(self.sup.slots):
                pid = s.info.get("pid") if s.state in ("idle", "busy") else None
                v = fdinfo_vram_gb(pid) if pid else None
                if v is not None:
                    any_, tot = True, tot + v
                    self.peak_by_worker[s.idx] = max(self.peak_by_worker.get(s.idx, 0.0), v)
            if any_:
                self.peak_total = max(self.peak_total or 0.0, round(tot, 3))

    def stop(self):
        self._stop.set()
        if self._t.is_alive():
            self._t.join(timeout=5)


# =================================================================================================================
# scoring (same definitions as eval/bench_tiered.run_mode)
# =================================================================================================================
class _Stub:
    def __init__(self, model_id: str, device: str):
        self.model_id, self.device = model_id, device


def failed_read(info: dict, model_id: str) -> dict:
    return {"fields": {}, "provenance": {}, "boxes": {}, "timing": {}, "ocr_lines": [], "model": model_id,
            "device": None, "cached": False, "read_failed": info}


def score_run(inbox: str, docs: list[dict], reads: dict, failed: dict, model_id: str, device: str,
              exp: dict | None, tag: str) -> tuple[dict, dict]:
    from eval.read_accuracy import score_reads
    from eval.run_eval import held_docs, score
    from ledger_room import pipeline, rules

    allr = {d["doc_id"]: reads.get(d["doc_id"]) or failed_read(failed.get(d["doc_id"]) or
                                                               {"reason": "no_reader", "detail": "no reading"}, model_id)
            for d in docs}
    run = pipeline.audit(inbox, _Stub(model_id, device), run_id=f"concurrency-{tag}-{datetime.now():%Y%m%d-%H%M%S}",
                         cache=False, read_fn=lambda ds, *a, **k: {d["doc_id"]: allr[d["doc_id"]] for d in ds})
    out = {"docs_for_person": held_docs(run),
           "read_review_not_read": sorted(e["doc_id"] for e in run["exceptions"] if e.get("reason")),
           "run_id": run["summary"]["run_id"]}
    if exp is not None:
        expd = {d["doc_id"]: set(d["issues"]) for d in exp["docs"]}
        pred: dict[str, set] = {}
        for e in run["exceptions"]:
            if e["rule_id"] in rules.ISSUE_TYPES:
                pred.setdefault(e["doc_id"], set()).add(e["rule_id"])
        amount_held = {e["doc_id"] for e in run["exceptions"] if e["rule_id"] == "AMOUNT_MISMATCH"}
        reading = score_reads({d["doc_id"]: d for d in run["docs"]}, exp, held=amount_held)
        sc = score(pred, expd)
        out.update(key_fields_wrong=reading["fields_wrong"], key_fields_compared=reading["fields_compared"],
                   key_field_error_rate=reading["field_error_rate"], rule_f1=sc["micro"]["f1"],
                   rule_micro=sc["micro"], clean_docs_flagged=sc["clean_docs_flagged"])
    else:
        out.update(key_fields_wrong=None, rule_f1=None, note="no answer key in the repository: counts and times only")
    return out, run


def fields_digest(res: dict) -> str:
    return hashlib.sha256(json.dumps(res.get("fields") or {}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def pct(vals: list[float], q: float) -> float | None:
    """Nearest-rank percentile."""
    if not vals:
        return None
    v = sorted(vals)
    return round(v[max(0, math.ceil(q * len(v)) - 1)], 2)


# =================================================================================================================
# one K
# =================================================================================================================
def parse_smi_ids(text: str | None, devices: list[str]) -> dict[str, str]:
    """GPU.N -> N unless given as GPU.0=1,GPU.1=0."""
    ids = {}
    for d in devices:
        m = re.match(r"GPU\.(\d+)$", d)
        ids[d] = m.group(1) if m else ("0" if d == "GPU" else None)
    for part in (text or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            ids[k.strip()] = v.strip()
    return {k: v for k, v in ids.items() if v is not None and k in devices}


def run_k(k: int, a, model_dir: str, docs: list[dict], inbox: str, exp, private: bool, out_dir: Path,
          k1: dict | None) -> dict:
    devices = [a.devices[i % len(a.devices)] for i in range(k)]
    row: dict = {"k": k, "worker_devices": devices,
                 "workers_per_device": {d: devices.count(d) for d in dict.fromkeys(devices)}}
    smi_ids = parse_smi_ids(a.xpu_smi_ids, list(dict.fromkeys(devices)))
    sampler = None
    if not a.no_power and smi_ids:
        sampler = XpuSampler(smi_ids, cmd=a.xpu_smi_cmd.split() if a.xpu_smi_cmd else None).start()
    t_idle0 = time.time()
    if sampler is not None and sampler.available() and a.idle_s > 0:
        time.sleep(a.idle_s)   # idle baseline: cards on, no worker loaded
    t_idle1 = time.time()
    sup = Supervisor(a.factory, model_dir, devices, str(out_dir), doc_timeout_s=a.doc_timeout_s,
                     load_timeout_s=a.load_timeout_s, max_restarts=a.max_restarts)
    fd = FdinfoSampler(sup).start()
    try:
        t = time.perf_counter()
        row["workers"] = sup.start()
        row["load_wall_s"] = round(time.perf_counter() - t, 1)
        row["workers_loaded"] = sum(1 for w in row["workers"] if w["state"] == "idle")
        if row["workers_loaded"] == 0:
            row["error"] = "no worker could load: " + "; ".join(e["error"] for e in sup.load_errors)[:500]
            return row
        model_id = next(w["model_id"] for w in row["workers"] if w.get("model_id"))
        e0 = sampler.read_energy() if sampler is not None else {}
        t_run0 = time.time()
        res = sup.run(docs)
        t_run1 = time.time()
        e1 = sampler.read_energy() if sampler is not None else {}
    finally:
        fd.stop()
        sup.close()
        if sampler is not None:
            sampler.stop()

    n = len(docs)
    n_read = len(res["reads"])   # throughput and energy count documents actually read
    wall = res["wall_s"]
    lat = list(res["latency_s"].values())
    row.update({
        "docs": n, "docs_read": len(res["reads"]), "docs_not_read": len(res["failed"]),
        "wall_s": round(wall, 1), "docs_per_hour": round(3600 * n_read / wall) if wall > 0 else None,
        "latency_s": {"mean": round(statistics.mean(lat), 2) if lat else None, "p50": pct(lat, 0.5),
                      "p95": pct(lat, 0.95), "max": round(max(lat), 2) if lat else None, "n": len(lat),
                      "definition": "seconds a worker spent on one document (OCR + model + post-processing)"},
        "docs_per_worker": res["docs_per_worker"],
        "crashes": len(res["crashes"]),
        "crash_log": [_private_safe(c, private) for c in res["crashes"]],
        "restarts": len(res["restarts"]),
        "restart_load_s": round(sum(r.get("load_s") or 0 for r in res["restarts"]), 1),
        "retries": [_private_safe(r, private) for r in res["retries"]],
        "not_read": {("(private)" if private else d): f for d, f in res["failed"].items()},
        "load_errors": sup.load_errors,
    })
    if k1 and k1.get("docs_per_hour") and row["docs_per_hour"]:
        row["speedup_vs_k1"] = round(row["docs_per_hour"] / k1["docs_per_hour"], 2)

    # memory
    mem: dict = {"fdinfo_peak_vram_gb_all_workers": fd.peak_total,
                 "fdinfo_peak_vram_gb_by_worker": {str(i): round(v, 2) for i, v in fd.peak_by_worker.items()} or None}
    pw: dict = {"idle_s": round(t_idle1 - t_idle0, 1)}
    if sampler is not None and sampler.available():
        idle = sampler.window(t_idle0, t_idle1)
        allw = sampler.window(t_idle0, t_run1)
        runw = sampler.window(t_run0, t_run1)
        mem["xpu_smi_idle_used_gb"] = {d: mem_mean_gb(v) for d, v in idle.items()}
        mem["xpu_smi_peak_used_gb"] = {d: mem_peak_gb(v) for d, v in allw.items()}
        per = {}
        for d, cnt in row["workers_per_device"].items():
            pk, base = mem["xpu_smi_peak_used_gb"].get(d), mem["xpu_smi_idle_used_gb"].get(d) or 0.0
            per[d] = round((pk - base) / cnt, 2) if pk is not None and cnt else None
        mem["xpu_smi_per_worker_gb_est"] = per
        by_dev = {d: power_summary(v) for d, v in runw.items()}
        idle_by_dev = {d: power_summary(v) for d, v in idle.items()}
        tot = [x["mean_w"] for x in by_dev.values()]
        mean_total = round(sum(tot), 2) if tot and all(x is not None for x in tot) else None
        itot = [x["mean_w"] for x in idle_by_dev.values()]
        idle_total = round(sum(itot), 2) if itot and all(x is not None for x in itot) else None
        pw.update({"by_device": by_dev, "idle_by_device": idle_by_dev, "mean_w_total": mean_total,
                   "idle_w_total": idle_total, **combine_energy(e0, e1, by_dev, mean_total, wall, n_read),
                   "marginal_wh_per_1000_docs": energy(mean_total - idle_total, wall, n_read)["wh_per_1000_docs"]
                   if mean_total is not None and idle_total is not None else None,
                   "source": sampler.mode, "raw_head": sampler.raw_head, "xpu_smi_ids": smi_ids,
                   "notes": sampler.notes})
        if mean_total is None:
            pw["note"] = "xpu-smi gave no power value (field missing?): see raw_head"
    else:
        mem["xpu_smi_peak_used_gb"] = None
        pw.update({"mean_w_total": None, "energy_wh": None, "wh_per_1000_docs": None,
                   "note": "power not measured: " + ("--no-power" if a.no_power else
                                                     "; ".join(sampler.notes) if sampler else "no xpu-smi id")})
    row["memory"] = mem
    row["power"] = pw

    # scoring
    sc, run = score_run(inbox, docs, res["reads"], res["failed"], model_id, ",".join(dict.fromkeys(devices)), exp,
                        f"k{k}")
    row["scoring"] = sc
    row["_digests"] = {d: fields_digest(r) for d, r in res["reads"].items()}
    return row


def _private_safe(e: dict, private: bool) -> dict:
    return {k: v for k, v in e.items() if not (private and k == "doc_id")}


def compare_to_k1(row: dict, k1: dict) -> None:
    a, b = row.get("scoring") or {}, k1.get("scoring") or {}
    keys = ("key_fields_wrong", "rule_f1", "docs_for_person")
    row["same_scoring_as_k1"] = all(a.get(x) == b.get(x) for x in keys)
    d1, d2 = k1.get("_digests") or {}, row.get("_digests") or {}
    row["docs_fields_differ_vs_k1"] = sum(1 for d in set(d1) & set(d2) if d1[d] != d2[d])


# =================================================================================================================
# main
# =================================================================================================================
def isolate_outputs() -> Path:
    from ledger_room import pipeline
    out = pipeline.DATA / "out" / "bench_concurrency"
    pipeline.OUT = out
    return out


def smi_discovery(cmd: str | None) -> str | None:
    c = (cmd.split() if cmd else ["xpu-smi"])
    if not shutil.which(c[0]) and not Path(c[0]).exists():
        return None
    try:
        r = subprocess.run(c + ["discovery"], capture_output=True, text=True, timeout=30)
        return (r.stdout + r.stderr)[:4000]
    except (OSError, subprocess.SubprocessError) as ex:
        return f"failed: {ex}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="K reader processes per card (see module docstring)")
    ap.add_argument("--set", default="heldout")
    ap.add_argument("--k", default=None, help="comma list of worker counts (default 1,2,3,4; dry run 1,2)")
    ap.add_argument("--devices", default=None, help="comma list, workers round robin (default GPU.0; dry run CPU)")
    ap.add_argument("--model", default=None, help="2b, 4b, 9b, 27b or a model folder (default 9b; dry run 2b)")
    ap.add_argument("--models-root", default=None)
    ap.add_argument("--n", type=int, default=None, help="first N documents of the set (dry run default 3)")
    ap.add_argument("--dry-run", action="store_true", help="laptop plumbing check; writes results_concurrency_dryrun.json")
    ap.add_argument("--doc-timeout-s", type=float, default=600, help="a worker silent this long on one document is "
                                                                     "killed and counted as a crash")
    ap.add_argument("--load-timeout-s", type=float, default=900)
    ap.add_argument("--max-restarts", type=int, default=50, help="per K run")
    ap.add_argument("--xpu-smi-ids", default=None, help="e.g. GPU.0=0,GPU.1=1 (default GPU.N -> N)")
    ap.add_argument("--xpu-smi-cmd", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--no-power", action="store_true")
    ap.add_argument("--idle-s", type=float, default=10, help="idle power/memory baseline before each K (seconds)")
    ap.add_argument("--settle-s", type=float, default=5, help="pause between K runs so device memory is freed")
    ap.add_argument("--factory", default=DEFAULT_FACTORY, help=argparse.SUPPRESS)
    ap.add_argument("--no-inventory", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--out", default=None)
    ap.add_argument("--note", default=None)
    a = ap.parse_args(argv)

    import ledger_room  # noqa: F401  (CI=true before openvino is imported)
    from eval import bench_b70 as B
    from eval.bench_tiered import expected_for, model_present, resolve_model, subset_inbox
    from ledger_room.pipeline import git_rev, load_inbox

    ks = [int(x) for x in (a.k or ("1,2" if a.dry_run else "1,2,3,4")).split(",") if x.strip()]
    a.devices = [x.strip() for x in (a.devices or ("CPU" if a.dry_run else "GPU.0")).split(",") if x.strip()]
    models_root = Path(a.models_root).expanduser() if a.models_root else B.default_models_root()
    n = a.n if a.n is not None else (3 if a.dry_run else None)
    out_dir = isolate_outputs()
    out_path = ROOT / "eval" / (a.out or ("results_concurrency_dryrun.json" if a.dry_run else "results_concurrency.json"))

    if a.factory == DEFAULT_FACTORY:
        model_dir = resolve_model(a.model or ("2b" if a.dry_run else "9b"), models_root)
        if not model_present(model_dir):
            print(f"model not found: {model_dir}", file=sys.stderr)
            return 2
    else:
        model_dir = Path(a.model or "")

    inv = {} if a.no_inventory else B.inventory(models_root)
    if not a.no_inventory:
        try:   # PCI address per OpenVINO device, to match against `xpu-smi discovery`
            import openvino as ov
            core = ov.Core()
            for d in inv.get("devices", []):
                if d["id"].startswith("GPU"):
                    d["pci"] = B._try(lambda: str(core.get_property(d["id"], "DEVICE_PCI_INFO")))
        except Exception:   # noqa: BLE001
            pass
    devnames = {d["id"]: d.get("name") for d in inv.get("devices", [])}

    _, docs_all = load_inbox(a.set)
    inbox = subset_inbox(a.set, n) if n else a.set
    _, docs = load_inbox(inbox)
    private = a.set.startswith("private")
    exp = None if private else expected_for(a.set, {d["doc_id"] for d in docs})

    doc = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "code_rev": git_rev(),
        "dry_run": a.dry_run, "command": " ".join(["python -m eval.bench_concurrency"] + (argv or sys.argv[1:])),
        "set": a.set, "docs_in_set": len(docs_all), "docs_used": len(docs),
        "answer_key": f"eval/expected_{a.set}.json" if exp is not None else None,
        "model": model_dir.name, "devices": a.devices, "factory": a.factory,
        "inventory": {k: inv.get(k) for k in ("os", "host", "cpu", "ram_gb", "openvino", "openvino_genai", "devices",
                                               "kernel")},
        "xpu_smi_discovery": None if a.no_power else smi_discovery(a.xpu_smi_cmd),
        "definitions": {
            "docs_per_hour": "documents read / read wall clock (all workers loaded -> last document back; reloads after a "
                             "crash included, first loads not)",
            "latency_s": "per document, seconds the worker spent on it (OCR + model + post-processing); the queue "
                         "wait is not in it",
            "wh_per_1000_docs": "energy over the run / documents read x 1000; energy from the xpu-smi energy counter "
                                "(end - start, all cards in use) when it behaves as a running counter, else mean "
                                "sampled power x read wall clock (energy_source says which; sampled_* always kept); "
                                "marginal_wh_per_1000_docs subtracts the idle power measured just before",
            "memory": "xpu_smi_peak_used_gb: device memory used (whole card, all processes), peak over load + run; "
                      "per_worker_gb_est = (peak - idle) / workers on that card; fdinfo_*: the workers' own DRM "
                      "fdinfo drm-total-vram (Linux)",
            "scoring": "pipeline.audit on the collected reads, then eval.read_accuracy and eval.run_eval.score "
                       "(as bench_tiered); same_scoring_as_k1 compares key_fields_wrong, rule_f1, docs_for_person",
            "supervisor": "a worker that dies is restarted and its document retried once; a second failure on the "
                          "same document sends it to a person (READ_REVIEW, reason reader_crash); no fixed reloads",
        },
        "notes": [],
        "rows": [],
    }
    if a.note:
        doc["notes"].append(a.note)
    if any("CPU" == d for d in a.devices):
        doc["notes"].append("CPU workers: no xpu-smi power or device memory")
    doc["notes"].append("OCR runs on the CPU inside each worker, so K workers also share the CPU")
    if private:
        doc["private"] = "aggregates only; no document ids"

    def save():
        doc["measured_on_b70"] = (not a.dry_run and bool(doc["rows"]) and
                                  all("B70" in (devnames.get(d) or "") for d in a.devices))
        rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in doc["rows"]]
        out_path.write_text(json.dumps({**doc, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"{a.set}: {len(docs)} documents, model {model_dir.name}, devices {a.devices}, K = {ks}", flush=True)
    k1 = None
    for i, k in enumerate(ks):
        if i and a.settle_s > 0:
            time.sleep(a.settle_s)
        print(f"\n== K = {k} ==", flush=True)
        try:
            row = run_k(k, a, str(model_dir), docs, inbox, exp, private, out_dir, k1)
        except Exception as ex:   # record and go on with the next K (e.g. out of device memory)
            row = {"k": k, "error": f"{type(ex).__name__}: {ex}"[:500]}
        if k == 1 and "error" not in row:
            k1 = row
        if k1 is not None and row is not k1 and "error" not in row:
            compare_to_k1(row, k1)
            if not row["same_scoring_as_k1"]:
                doc["notes"].append(f"K={k}: scoring differs from K=1 (see rows)")
        if private:
            row.pop("_digests", None)
        doc["rows"].append(row)
        save()
        s = row.get("scoring") or {}
        print(f"  K={k}: {row.get('docs_per_hour')}/h, p95 {((row.get('latency_s') or {}).get('p95'))} s, "
              f"crashes {row.get('crashes')}, not read {row.get('docs_not_read')}, fields wrong "
              f"{s.get('key_fields_wrong')}, F1 {s.get('rule_f1')}, "
              f"W {(row.get('power') or {}).get('mean_w_total')}, "
              f"Wh/1000 {(row.get('power') or {}).get('wh_per_1000_docs')}, "
              f"mem {(row.get('memory') or {}).get('xpu_smi_peak_used_gb')}"
              + (f"  ERROR {row['error']}" if row.get("error") else ""), flush=True)
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
