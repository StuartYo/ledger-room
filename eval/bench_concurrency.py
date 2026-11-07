"""Several readers per card: K independent reader processes on one (or two) B70 cards -> eval/results_concurrency.json

  .venv/bin/python -m eval.bench_concurrency --set heldout --k 1,2,3,4 --devices GPU.0 --models-root ~/models
  .venv/bin/python -m eval.bench_concurrency --set heldout --k 2,4,6,8 --devices GPU.0,GPU.1 --models-root ~/models --out results_concurrency_2cards.json
  python -m eval.bench_concurrency --dry-run          # laptop plumbing check: 2B on CPU, 3 documents, K = 1,2

Long run (sustained, -> eval/results_longrun.json): the public sets with an answer key (data/heldout, hard, blind3,
main; also heldout2, control_before, control_after, real_tw; private_* is refused), pass after pass for a fixed time.
At about 1,000 documents an hour per card (results_concurrency_2cards.json), 70 minutes clears 1,000 per card:
  .venv/bin/python -m eval.bench_concurrency --sets heldout,hard,blind3,main --k 2 --devices GPU.0,GPU.1 \
      --duration-min 70 --models-root ~/models --out results_longrun.json
  .venv/bin/python -m eval.bench_concurrency --sets heldout --repeat 3 --k 1 --devices GPU.0 --models-root ~/models
  python -m eval.bench_concurrency --dry-run --sets heldout,hard --repeat 2 --k 2   # laptop plumbing check
  --repeat N        N passes over the sets (each set of each pass is its own scoring block: F1, key fields wrong and
                    docs for a person per pass, compared with the first complete pass of that set)
  --duration-min M  keep cycling through the sets until M minutes of read wall clock have passed; the documents
                    already handed out are finished, nothing new starts (with --repeat too: whichever comes first)
  Per card: documents read, wall clock, docs/hour over the whole run, crashes and restarts per hour, documents sent
  to a person because the reader failed, Wh per 1,000 documents, GPU utilisation. Reads go to side files under
  data/out/bench_concurrency/longrun/<time>-k<K>/ (one JSONL per pass and set, plus the full 1-second xpu-smi series
  as CSV); the supervisor keeps only counts, and the results file is kept under 2 MB (fit_json).

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

Power, memory and GPU utilisation come from Intel's xpu-smi (see XpuSampler: the exact commands and field names are
listed there and recorded in the results file). Utilisation (metric 0) is what shows whether one reader already keeps
a card busy: per card and K, mean, p50, p95 and the share of 1-second samples at or above 90 %. xpu-smi device ids are assumed to be GPU.N -> N; override with --xpu-smi-ids
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
        self.attempts: dict[str, int] = {}   # per document key; dropped once the document is finished
        self.pending: deque = deque()        # retries only (they go before new documents)
        self.retries: list[dict] = []
        self._next = lambda: None
        self._fed_all = True
        self._sink = self._store

    def _store(self, d: dict, ok: bool, value, secs, worker) -> None:
        """Default sink of run(docs): keep every read in memory (fine for one pass over one set)."""
        if ok:
            self.reads[d["doc_id"]] = value
            self.latency[d["doc_id"]] = secs
        else:
            self.failed[d["doc_id"]] = value

    @staticmethod
    def _key(d: dict) -> str:
        return d.get("key", d["doc_id"])   # a long run feeds the same doc_id once per pass: key = pass|set|doc_id

    def _take(self) -> dict | None:
        if self.pending:
            return self.pending.popleft()
        if self._fed_all:
            return None
        d = self._next()
        if d is None:
            self._fed_all = True
        return d

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
    def run(self, docs: list[dict], *, feed=None, sink=None, drain_on_abort: bool = True) -> dict:
        """Read docs (or, with feed, whatever feed() hands out until it returns None).
        sink(doc, ok, read_or_failure, latency_s, worker_idx) gets every finished document instead of the in-memory
        reads/failed dicts (a long run streams them to disk). drain_on_abort: when no worker is left, also pull the
        rest of the feed and send it to a person ("no_reader"); a long run stops feeding instead."""
        self._reset_run()
        if feed is None:
            it = iter(docs)

            def feed():
                return next(it, None)
        self._next, self._fed_all = feed, False
        if sink is not None:
            self._sink = sink
        crashes0, restarts0 = len(self.crashes), len(self.restarts)
        aborted = False
        t_run = time.perf_counter()
        while True:
            for s in self.slots:
                if s.state != "idle":
                    continue
                d = self._take()
                if d is None:
                    break
                try:
                    s.conn.send({**d, "path": str(d["path"])})
                    s.state, s.doc, s.t_state = "busy", d, time.perf_counter()
                except (OSError, EOFError, BrokenPipeError):   # died while idle: _step sees it
                    self.pending.appendleft(d)
            busy = [s for s in self.slots if s.state == "busy"]
            if not self.pending and not busy and self._fed_all:
                break
            if not self._live():
                aborted = True
                rest = list(self.pending)
                self.pending.clear()
                if drain_on_abort:
                    d = self._take()
                    while d is not None:
                        rest.append(d)
                        d = self._take()
                else:
                    self._fed_all = True
                for d in rest:
                    self._fail_final(d, "no_reader", "every reader process had stopped and could not be restarted")
                break
            self._step()
        wall = time.perf_counter() - t_run
        return {"reads": self.reads, "failed": self.failed, "latency_s": self.latency, "wall_s": wall,
                "crashes": self.crashes[crashes0:], "restarts": self.restarts[restarts0:], "retries": self.retries,
                "docs_per_worker": {s.idx: s.docs_done for s in self.slots}, "aborted": aborted}

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
            d = s.doc or {"doc_id": doc_id}
            self.attempts.pop(self._key(d), None)
            s.docs_done += 1
            s.state, s.doc = "idle", None
            self._sink(d, True, res, secs, s.idx)
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
        rec["attempt"] = self.attempts.get(self._key(d), 0) + 1 if d else None
        self.crashes.append(rec)
        what = f"timeout after {self.doc_timeout_s} s" if timeout else str(_exit_text(code))
        self.log(f"  [crash] worker {s.idx} ({s.device}, pid {rec['pid']}) {what} on doc "
                 f"{d['doc_id'] if d else '(idle)'}")
        if d is not None:
            self._fail(d, "reader_crash", ("timeout" if timeout else f"exit {code}") +
                       (f" {rec['signal']}" if rec.get("signal") else ""), worker=s.idx)
        self._restart(s, f"crash on {d['doc_id']}" if d else "crash while idle")

    def _fail(self, d: dict, reason: str, detail: str, worker: int) -> None:
        key = self._key(d)
        n = self.attempts[key] = self.attempts.get(key, 0) + 1
        if n < 2:
            self.retries.append({"doc_id": d["doc_id"], "reason": reason, "detail": detail, "worker": worker,
                                 **({"key": d["key"]} if "key" in d else {})})
            self.log(f"  [retry] {d['doc_id']} once more ({reason}: {detail})")
            self.pending.appendleft(d)
        else:
            self._fail_final(d, reason, f"failed twice; last: {detail}", worker=worker)

    def _fail_final(self, d: dict, reason: str, detail: str, worker: int | None = None) -> None:
        info = {"reason": reason, "detail": detail, "attempts": self.attempts.pop(self._key(d), 0)}
        self.log(f"  [to a person] {d['doc_id']} READ_REVIEW ({reason}: {detail})")
        self._sink(d, False, info, None, worker)

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
#   xpu-smi dump --device <id> --metrics 0,1,18 --interval 1 --number 1000000
#     (long flags only: this build rejects -m / -i / -n; `stdbuf -oL` in front when stdbuf exists so rows are not
#      held in a pipe buffer; the process is terminated when the K run ends)
#     metric 0 = GPU utilisation, 1 = power, 18 = memory used (0 not yet seen on the host: if the dump with 0 gives
#     no row, it is run again with 1,18). Output: a whitespace-aligned table (older builds: CSV), e.g.
#       Timestamp      DeviceId    power.draw (W)    memory.used (MiB)
#       18:03:53...           0             76.83         1547.6328125
#     (the timestamp is truncated with "..."; it is not used, samples are stamped on arrival)
#     Columns are found by header name, never by position: power = a header holding "power" and "(W)"
#     ("power.draw (W)", old "GPU Power (W)"); memory = "memory.used" / "Memory Used" (unit from the parentheses:
#     MiB / GiB / MB / KiB / B; MiB if none); utilisation = a header holding "util" and "gpu" but not "mem"
#     (classic "GPU Utilization (%)"; a dotted build probably "utilization.gpu (%)"), its name and the raw header
#     line are stored, null when no column matches. Header cells are split on commas or on 2+ spaces, data rows on
#     commas or any whitespace. "N/A" or an empty cell -> no value for that sample.
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


def is_gpu_util(name: str) -> bool:
    """GPU utilisation column / key: the name holds "util" and "gpu" but not "mem" (classic "GPU Utilization (%)",
    dotted builds probably "utilization.gpu (%)" or "gpu.utilization (%)"; "GPU Memory Utilization (%)" is not it)."""
    lc = str(name).lower()
    return "util" in lc and "gpu" in lc and "mem" not in lc


def dump_columns(header_line: str) -> dict | None:
    """Column positions in an xpu-smi dump header, by name. None if the line is not a header."""
    sep = r"\s*,\s*" if "," in header_line else r"\s{2,}"
    cols = [c.strip() for c in re.split(sep, header_line.strip()) if c.strip()]
    if not any(c.lower().startswith("timestamp") for c in cols):
        return None
    out = {"names": cols, "raw": header_line.rstrip()[:300]}
    for i, c in enumerate(cols):
        lc = c.lower()
        if "power" in lc and "(w)" in lc and "power" not in out:
            out["power"] = i
        elif _MEM_RE.search(c) and "memory" not in out:
            out["memory"], out["memory_to_gb"] = i, _mem_unit(c)
        elif is_gpu_util(c) and "util" not in out:
            out["util"] = i
        elif lc.startswith("timestamp"):
            out["timestamp"] = i
    return out


def parse_dump_row(cols: dict, line: str) -> dict | None:
    cells = [c.strip() for c in (line.split(",") if "," in line else line.split())]
    if len(cells) < 2 or not re.match(r"^\d", cells[0]):
        return None

    def cell(name):
        return _num(cells[cols[name]]) if name in cols and cols[name] < len(cells) else None
    m = cell("memory")
    return {"power_w": cell("power"), "mem_gb": round(m * cols["memory_to_gb"], 3) if m is not None else None,
            "util_pct": cell("util")}


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


def _tile_mean(v) -> float | None:
    """Utilisation over tiles is a mean, not a sum."""
    if isinstance(v, dict):
        if any(k in v for k in ("current", "avg")):
            return _num(v.get("current", v.get("avg")))
        tiles = [t for t in (_tile_mean(x) for x in v.values()) if t is not None]
        return round(statistics.mean(tiles), 2) if tiles else None
    return None if isinstance(v, list) else _num(v)


def _util_from_json(obj) -> float | None:
    """GPU utilisation % from the first key that is_gpu_util() (searched at any depth)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if is_gpu_util(k):
                r = _tile_mean(v)
                if r is not None:
                    return r
        for v in obj.values():
            r = _util_from_json(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _util_from_json(v)
            if r is not None:
                return r
    return None


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
    out = {"power_w": None, "mem_gb": None, "energy_j": None, "util_pct": None}
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
            elif t == "XPUM_STATS_GPU_UTILIZATION" and out["util_pct"] is None:
                out["util_pct"] = v
        if out["mem_gb"] is None:
            out["mem_gb"] = _mem_from_json(js)
        if out["util_pct"] is None:
            out["util_pct"] = _util_from_json(js)
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
        elif is_gpu_util(key) and out["util_pct"] is None:
            out["util_pct"] = _num(cells[1])
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


class RunningStats:
    """Exact summary of a sample stream without keeping the samples: power count / sum / max, memory peak, GPU
    utilisation mean, share of samples >= 90 % and a 1 %-bin histogram for p50 / p95 (101 ints, whatever the length
    of the run)."""

    def __init__(self):
        self.p_n, self.p_sum, self.p_max = 0, 0.0, None
        self.m_max = None
        self.u_n, self.u_sum, self.u_ge90 = 0, 0.0, 0
        self.u_hist = [0] * 101

    def add(self, power=None, mem=None, util=None) -> None:
        if power is not None:
            self.p_n, self.p_sum = self.p_n + 1, self.p_sum + power
            self.p_max = power if self.p_max is None else max(self.p_max, power)
        if mem is not None:
            self.m_max = mem if self.m_max is None else max(self.m_max, mem)
        if util is not None:
            self.u_n, self.u_sum = self.u_n + 1, self.u_sum + util
            self.u_ge90 += util >= 90
            self.u_hist[min(100, max(0, int(round(util))))] += 1

    @classmethod
    def of(cls, samples) -> "RunningStats":
        r = cls()
        for smp in samples:
            r.add(smp[1], smp[2], smp[3] if len(smp) > 3 else None)
        return r

    def _hist_pct(self, q: float) -> int | None:
        if not self.u_n:
            return None
        target, cum = max(1, math.ceil(q * self.u_n)), 0
        for v, c in enumerate(self.u_hist):
            cum += c
            if cum >= target:
                return v
        return 100

    def power(self) -> dict:
        return {"mean_w": round(self.p_sum / self.p_n, 2) if self.p_n else None,
                "max_w": round(self.p_max, 2) if self.p_max is not None else None, "samples": self.p_n}

    def util(self) -> dict:
        n = self.u_n
        return {"mean_pct": round(self.u_sum / n, 1) if n else None, "p50_pct": self._hist_pct(0.5),
                "p95_pct": self._hist_pct(0.95), "frac_ge_90": round(self.u_ge90 / n, 3) if n else None,
                "samples": n}

    def mem_peak_gb(self) -> float | None:
        return round(self.m_max, 2) if self.m_max is not None else None


def util_summary(samples: list[tuple]) -> dict:
    """GPU utilisation over (t, power, mem, util) samples: mean, p50, p95 (1 % bins), share of samples >= 90 %."""
    return RunningStats.of(samples).util()


class XpuSampler:
    """Background sampling of power, memory and GPU utilisation per device, about once a second (one thread per
    device).

    Memory stays bounded however long the run: every sample goes (1) into an exact running summary (RunningStats,
    reset_acc / acc_snapshot), (2) at full resolution to a side CSV file when spill_dir is given, and (3) into the
    in-RAM list used by window(), which is thinned to every 2nd sample (and from then on keeps every 2nd, 4th, ...
    sample) whenever it passes max_keep. window() over a long span is therefore a uniform subsample."""

    def __init__(self, smi_ids: dict[str, str], *, interval_s: float = 1.0, cmd: list[str] | None = None,
                 metrics: str = "0,1,18", max_keep: int = 4000, spill_dir: Path | None = None):
        self.smi_ids, self.interval_s, self.metrics = dict(smi_ids), interval_s, metrics
        self.cmd = list(cmd) if cmd else ["xpu-smi"]
        self.max_keep = max(2, int(max_keep))
        self.samples: dict[str, list[tuple]] = {d: [] for d in smi_ids}   # (time.time(), W, GB, util %)
        self.stride: dict[str, int] = {d: 1 for d in smi_ids}
        self._seen: dict[str, int] = {d: 0 for d in smi_ids}
        self.acc: dict[str, RunningStats] = {d: RunningStats() for d in smi_ids}
        self.raw_head: dict[str, list[str]] = {d: [] for d in smi_ids}
        self.header: dict[str, str | None] = {d: None for d in smi_ids}       # raw dump header line
        self.util_column: dict[str, str | None] = {d: None for d in smi_ids}  # header name used for utilisation
        self.mode: dict[str, str] = {}
        self.notes: list[str] = []
        self.spill_dir = Path(spill_dir) if spill_dir else None
        self.spill_files: dict[str, str] = {}
        self._spill: dict = {}
        self._stop = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def available(self) -> bool:
        return shutil.which(self.cmd[0]) is not None or Path(self.cmd[0]).exists()

    def dump_cmd(self, smi_id: str, metrics: str | None = None) -> list[str]:
        pre = ["stdbuf", "-oL"] if self.cmd == ["xpu-smi"] and shutil.which("stdbuf") else []
        return pre + self.cmd + ["dump", "--device", str(smi_id), "--metrics", metrics or self.metrics,
                                 "--interval", str(max(1, round(self.interval_s))), "--number", "1000000"]

    def stats_cmd(self, smi_id: str) -> list[str]:
        return self.cmd + ["stats", "-d", str(smi_id), "-j"]

    def start(self) -> "XpuSampler":
        if not self.available():
            self.notes.append(f"{self.cmd[0]} not found: power, utilisation and xpu-smi memory not measured")
            return self
        self.open_spill()
        for dev, sid in self.smi_ids.items():
            th = threading.Thread(target=self._run_device, args=(dev, sid), daemon=True, name=f"xpu-smi-{dev}")
            th.start()
            self._threads.append(th)
        return self

    def open_spill(self) -> None:
        """One CSV per device with every sample (unix_time, W, GB, util %), when spill_dir is set."""
        if self.spill_dir is None or self._spill:
            return
        self.spill_dir.mkdir(parents=True, exist_ok=True)
        for dev in self.smi_ids:
            f = self.spill_dir / f"xpu_smi_{re.sub(r'[^A-Za-z0-9]+', '_', dev)}.csv"
            self._spill[dev] = f.open("w", encoding="utf-8", buffering=1)
            self._spill[dev].write("unix_time,power_w,mem_gb,util_pct\n")
            self.spill_files[dev] = str(f)

    def _add(self, dev: str, power, mem, util=None) -> None:
        t = time.time()
        with self._lock:
            self.acc[dev].add(power, mem, util)
            f = self._spill.get(dev)
            if f is not None:
                f.write(",".join("" if v is None else str(v) for v in (round(t, 2), power, mem, util)) + "\n")
            self._seen[dev] += 1
            if self._seen[dev] % self.stride[dev]:
                return
            lst = self.samples[dev]
            lst.append((t, power, mem, util))
            if len(lst) > self.max_keep:
                self.samples[dev] = lst[::2]
                self.stride[dev] *= 2

    def reset_acc(self) -> None:
        with self._lock:
            self.acc = {d: RunningStats() for d in self.smi_ids}

    def acc_snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {d: {"power": a.power(), "util": a.util(), "mem_peak_gb": a.mem_peak_gb()}
                    for d, a in self.acc.items()}

    def _head(self, dev: str, line: str) -> None:
        if len(self.raw_head[dev]) < 4:
            self.raw_head[dev].append(line.rstrip()[:300])

    def _dump(self, dev: str, sid: str, metrics: str) -> int:
        got = 0
        cmd = self.dump_cmd(sid, metrics)
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            self._procs.append(p)
            self.mode[dev] = "dump: " + " ".join(cmd)
            cols = None
            for line in p.stdout:
                if self._stop.is_set():
                    break
                self._head(dev, line)
                if cols is None:
                    cols = dump_columns(line)
                    if cols is not None:
                        self.header[dev] = cols["raw"]
                        self.util_column[dev] = cols["names"][cols["util"]] if "util" in cols else None
                    continue
                r = parse_dump_row(cols, line)
                if r is not None:
                    got += 1
                    self._add(dev, r["power_w"], r["mem_gb"], r["util_pct"])
        except OSError as ex:
            self.notes.append(f"{dev}: xpu-smi dump could not start: {ex}")
        return got

    def _run_device(self, dev: str, sid: str) -> None:
        # metric 0 (GPU utilisation) first; if this build rejects it, the old power + memory pair still runs
        for metrics in dict.fromkeys([self.metrics, "1,18"]):
            if self._dump(dev, sid, metrics) or self._stop.is_set():
                return
            self.notes.append(f"{dev}: xpu-smi dump --metrics {metrics} gave no sample rows")
        self.notes.append(f"{dev}: polling `xpu-smi stats -j` instead")
        self.mode[dev] = "stats: " + " ".join(self.stats_cmd(sid))
        while not self._stop.is_set():
            t = time.time()
            try:
                txt = subprocess.run(self.stats_cmd(sid), capture_output=True, text=True, timeout=10).stdout
                self._head(dev, txt[:300])
                r = parse_stats(txt)
                if any(r[x] is not None for x in ("power_w", "mem_gb", "util_pct")):
                    self._add(dev, r["power_w"], r["mem_gb"], r["util_pct"])
            except (OSError, subprocess.SubprocessError) as ex:
                self.notes.append(f"{dev}: xpu-smi stats failed: {ex}"[:200])
                return
            self._stop.wait(max(0.0, self.interval_s - (time.time() - t)))

    def close_spill(self) -> None:
        with self._lock:
            for f in self._spill.values():
                try:
                    f.close()
                except OSError:
                    pass
            self._spill = {}

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
        self.close_spill()

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


def util_block(sampler: "XpuSampler", dev: str, summary: dict) -> dict:
    """Utilisation summary plus where it came from. No utilisation column -> nulls, with the raw header kept so the
    field name can be checked by hand."""
    col = sampler.util_column.get(dev)
    out = {**summary, "column": col, "raw_header": sampler.header.get(dev)}
    if not summary.get("samples"):
        out["note"] = ("no GPU utilisation value: no header column holds 'util' and 'gpu' (see raw_header)"
                       if sampler.header.get(dev) and col is None else "no GPU utilisation samples")
    return out


def per_hour(n: int | float, wall_s: float) -> float | None:
    """Events per hour of wall clock: n x 3600 / seconds."""
    return round(n * 3600 / wall_s, 2) if wall_s and wall_s > 0 else None


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
        row["utilisation"] = {d: util_block(sampler, d, util_summary(v)) for d, v in runw.items()}
    else:
        mem["xpu_smi_peak_used_gb"] = None
        pw.update({"mean_w_total": None, "energy_wh": None, "wh_per_1000_docs": None,
                   "note": "power not measured: " + ("--no-power" if a.no_power else
                                                     "; ".join(sampler.notes) if sampler else "no xpu-smi id")})
        row["utilisation"] = None
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
# long run: several sets, several passes, or a fixed number of minutes (one row per K)
# =================================================================================================================
MAX_JSON_BYTES = 2_000_000
LOG_CAP = 100   # crash / restart / retry entries kept in the results file per row (the counts are always complete)


class LongFeed:
    """Hands out documents pass after pass, set after set: pass 1 = every set in order, then pass 2, ... Each
    (pass, set) is one scoring block. Stops at max_passes, or once the deadline has passed (the documents already
    handed out are finished; retries of them still run)."""

    def __init__(self, sets: list[dict], max_passes: int | None, blocks_dir: Path):
        self.sets, self.max_passes, self.blocks_dir = sets, max_passes, blocks_dir
        self.blocks: list[dict] = []
        self.deadline: float | None = None   # time.perf_counter() value
        self.stopped_by: str | None = None
        self._p, self._si, self._di = 1, 0, 0

    def __call__(self) -> dict | None:
        if self.stopped_by:
            return None
        if self.deadline is not None and time.perf_counter() >= self.deadline:
            self.stopped_by = "duration"
            return None
        while True:
            if self.max_passes is not None and self._p > self.max_passes:
                self.stopped_by = "repeat"
                return None
            st = self.sets[self._si]
            if self._di < len(st["docs"]):
                if self._di == 0:
                    self.blocks.append(self._new_block(st))
                b = self.blocks[-1]
                d = st["docs"][self._di]
                self._di += 1
                b["fed"] += 1
                if b["t_first"] is None:
                    b["t_first"] = time.time()
                return {**d, "key": f"{self._p}|{st['name']}|{d['doc_id']}", "block": len(self.blocks) - 1}
            self._si, self._di = self._si + 1, 0
            if self._si >= len(self.sets):
                self._si, self._p = 0, self._p + 1

    def _new_block(self, st: dict) -> dict:
        i = len(self.blocks)
        return {"pass": self._p, "set": st["name"], "si": self._si, "fed": 0, "done": 0, "read": 0, "not_read": 0,
                "reasons": {}, "lat": [], "t_first": None, "t_last": None,
                "file": self.blocks_dir / f"{i:04d}_p{self._p}_{st['name']}.jsonl"}


def score_blocks(blocks: list[dict], sets: list[dict], model_id: str, devices: str, tag: str, t_run0: float,
                 sampler: "XpuSampler | None") -> tuple[list[dict], dict]:
    """Score every (pass, set) block on its own (reads come back from the block's side file, so the supervisor
    never held them all), and compare each complete pass with the first complete pass of the same set."""
    from eval.bench_tiered import expected_for, subset_inbox
    out, first = [], {}
    for b in blocks:
        st = sets[b["si"]]
        reads, failed = {}, {}
        with open(b["file"], encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                (reads if r["ok"] else failed)[r["doc_id"]] = r["value"]
        partial = b["fed"] < len(st["docs"])
        docs_b = st["docs"][:b["fed"]]
        if partial:   # only the last block of a timed run (or of a run with no reader left)
            inbox = subset_inbox(st["name"], b["fed"])
            exp = expected_for(st["name"], {d["doc_id"] for d in docs_b}) if st["exp"] is not None else None
        else:
            inbox, exp = st["inbox"], st["exp"]
        sc, _ = score_run(inbox, docs_b, reads, failed, model_id, devices, exp,
                          f"{tag}-p{b['pass']}-{st['name']}")
        lat = b["lat"]
        rec = {"pass": b["pass"], "set": st["name"], "docs": b["fed"], "partial": partial,
               "docs_read": len(reads), "docs_not_read": len(failed),
               "not_read_reasons": dict(b["reasons"]) or None,
               "rule_f1": sc.get("rule_f1"), "key_fields_wrong": sc.get("key_fields_wrong"),
               "key_fields_compared": sc.get("key_fields_compared"), "docs_for_person": sc.get("docs_for_person"),
               "latency_s": {"p50": pct(lat, 0.5), "p95": pct(lat, 0.95)},
               "t_start_s": round(b["t_first"] - t_run0, 1) if b["t_first"] else None,
               "t_end_s": round(b["t_last"] - t_run0, 1) if b["t_last"] else None,
               "run_id": sc.get("run_id")}
        if sampler is not None and b["t_first"] and b["t_last"]:
            w = sampler.window(b["t_first"], b["t_last"])
            rec["util_mean_pct"] = {d: util_summary(v)["mean_pct"] for d, v in w.items()}
            rec["power_mean_w"] = {d: power_summary(v)["mean_w"] for d, v in w.items()}
        dig = {d: fields_digest(r) for d, r in reads.items()}
        if not partial:
            f0 = first.get(st["name"])
            if f0 is None:
                first[st["name"]] = {"digests": dig, **{k: rec[k] for k in
                                                        ("rule_f1", "key_fields_wrong", "docs_for_person")}}
                rec["first_complete_pass"] = True
            else:
                rec["same_scoring_as_first_pass"] = all(rec[k] == f0[k] for k in
                                                        ("rule_f1", "key_fields_wrong", "docs_for_person"))
                rec["docs_fields_differ_vs_first_pass"] = sum(1 for d in set(dig) & set(f0["digests"])
                                                              if dig[d] != f0["digests"][d])
        out.append(rec)

    stability = {}
    for st in sets:
        full = [r for r in out if r["set"] == st["name"] and not r["partial"]]
        if not full:
            stability[st["name"]] = {"complete_passes": 0}
            continue
        stability[st["name"]] = {
            "complete_passes": len(full),
            "rule_f1_values": list(dict.fromkeys(r["rule_f1"] for r in full)),
            "key_fields_wrong_values": list(dict.fromkeys(r["key_fields_wrong"] for r in full)),
            "docs_for_person_values": list(dict.fromkeys(r["docs_for_person"] for r in full)),
            "all_complete_passes_same_scoring": all(r.get("same_scoring_as_first_pass", True) for r in full),
            "max_docs_fields_differ_vs_first_pass": max(r.get("docs_fields_differ_vs_first_pass", 0) for r in full),
        }
    return out, stability


def run_longrun(k: int, a, model_dir: str, sets: list[dict], out_dir: Path) -> dict:
    """One sustained run with K workers: documents of all sets, pass after pass, until --repeat passes or
    --duration-min minutes of read wall clock are done."""
    devices = [a.devices[i % len(a.devices)] for i in range(k)]
    uniq = list(dict.fromkeys(devices))
    row: dict = {"k": k, "worker_devices": devices, "workers_per_device": {d: devices.count(d) for d in uniq}}
    side = out_dir / "longrun" / f"{datetime.now():%Y%m%d-%H%M%S}-k{k}"
    (side / "blocks").mkdir(parents=True, exist_ok=True)
    try:
        row["side_files"] = side.relative_to(ROOT).as_posix()
    except ValueError:
        row["side_files"] = str(side)
    smi_ids = parse_smi_ids(a.xpu_smi_ids, uniq)
    sampler = None
    if not a.no_power and smi_ids:
        sampler = XpuSampler(smi_ids, cmd=a.xpu_smi_cmd.split() if a.xpu_smi_cmd else None, spill_dir=side).start()
    have_smi = sampler is not None and sampler.available()
    t_idle0 = time.time()
    if have_smi and a.idle_s > 0:
        time.sleep(a.idle_s)
    t_idle1 = time.time()

    max_passes = a.repeat if a.repeat else (None if a.duration_min else 1)
    feed = LongFeed(sets, max_passes, side / "blocks")
    sup = Supervisor(a.factory, model_dir, devices, str(out_dir), doc_timeout_s=a.doc_timeout_s,
                     load_timeout_s=a.load_timeout_s, max_restarts=a.max_restarts)
    dev_of = {s.idx: s.device for s in sup.slots}
    by_dev = {d: {"read": 0, "not_read": 0} for d in uniq}
    no_dev = {"not_read": 0}

    def sink(d, ok, value, secs, worker):
        b = feed.blocks[d["block"]]
        dev = dev_of.get(worker)
        with open(b["file"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"doc_id": d["doc_id"], "ok": ok, "value": value, "latency_s": secs, "device": dev},
                               ensure_ascii=False, default=str) + "\n")
        b["done"] += 1
        b["t_last"] = time.time()
        if ok:
            b["read"] += 1
            b["lat"].append(secs)
            by_dev[dev]["read"] += 1
        else:
            b["not_read"] += 1
            b["reasons"][value["reason"]] = b["reasons"].get(value["reason"], 0) + 1
            (by_dev[dev] if dev in by_dev else no_dev)["not_read"] += 1
        if b["done"] == len(sets[b["si"]]["docs"]):
            print(f"  pass {b['pass']} {b['set']}: {b['read']}/{b['done']} read, "
                  f"{(time.time() - t_run0) / 60:.1f} min", flush=True)

    fd = FdinfoSampler(sup).start()
    t_run0 = time.time()
    try:
        t = time.perf_counter()
        row["workers"] = sup.start()
        row["load_wall_s"] = round(time.perf_counter() - t, 1)
        row["workers_loaded"] = sum(1 for w in row["workers"] if w["state"] == "idle")
        if row["workers_loaded"] == 0:
            row["error"] = "no worker could load: " + "; ".join(e["error"] for e in sup.load_errors)[:500]
            return row
        model_id = next(w["model_id"] for w in row["workers"] if w.get("model_id"))
        e0 = sampler.read_energy() if have_smi else {}
        if sampler is not None:
            sampler.reset_acc()
        t_run0 = time.time()
        if a.duration_min:
            feed.deadline = time.perf_counter() + a.duration_min * 60
        res = sup.run([], feed=feed, sink=sink, drain_on_abort=False)
        t_run1 = time.time()
        acc = sampler.acc_snapshot() if sampler is not None else {}
        e1 = sampler.read_energy() if have_smi else {}
    finally:
        fd.stop()
        sup.close()
        if sampler is not None:
            sampler.stop()

    wall = res["wall_s"]
    blocks = feed.blocks
    n_fed = sum(b["fed"] for b in blocks)
    n_read = sum(b["read"] for b in blocks)
    n_not = sum(b["not_read"] for b in blocks)
    crashes, restarts = res["crashes"], res["restarts"]
    lat = [x for b in blocks for x in b["lat"]]
    row.update({
        "stopped_by": "no_reader_left" if res["aborted"] else feed.stopped_by,
        "passes_started": max((b["pass"] for b in blocks), default=0), "blocks": len(blocks),
        "docs": n_fed, "docs_read": n_read, "docs_not_read_reader_failure": n_not,
        "wall_s": round(wall, 1), "wall_min": round(wall / 60, 1),
        "docs_per_hour": round(3600 * n_read / wall) if wall > 0 else None,
        "crashes": len(crashes), "crashes_per_hour": per_hour(len(crashes), wall),
        "restarts": len(restarts), "restarts_per_hour": per_hour(len(restarts), wall),
        "restarts_after_reader_error": sum(1 for r in restarts if r["reason"] == "after reader_error"),
        "restart_load_s": round(sum(r.get("load_s") or 0 for r in restarts), 1),
        "latency_s": {"mean": round(statistics.mean(lat), 2) if lat else None, "p50": pct(lat, 0.5),
                      "p95": pct(lat, 0.95), "max": round(max(lat), 2) if lat else None, "n": len(lat)},
        "crash_log": crashes[:LOG_CAP], "restart_log": restarts[:LOG_CAP], "retries": res["retries"][:LOG_CAP],
        "logs_capped_at": LOG_CAP, "retries_total": len(res["retries"]),
        "load_errors": sup.load_errors[:LOG_CAP],
        "not_read_no_device": no_dev["not_read"],
    })

    # per card
    idle = sampler.window(t_idle0, t_idle1) if have_smi else {}
    per_dev, by_dev_power = {}, {}
    for dev in uniq:
        nd = by_dev[dev]["read"]
        cr = sum(1 for c in crashes if c["device"] == dev)
        rs = sum(1 for r in restarts if r["device"] == dev)
        pd = {"workers": devices.count(dev), "docs_read": nd,
              "docs_not_read_reader_failure": by_dev[dev]["not_read"], "wall_s": round(wall, 1),
              "docs_per_hour": round(3600 * nd / wall) if wall > 0 else None,
              "crashes": cr, "crashes_per_hour": per_hour(cr, wall),
              "restarts": rs, "restarts_per_hour": per_hour(rs, wall),
              "reached_1000_docs": nd >= 1000}
        if have_smi and dev in acc:
            s = acc[dev]
            by_dev_power[dev] = s["power"]
            a0, a1 = (e0.get(dev) or {}), (e1.get(dev) or {})
            dj, why = energy_from_counter(a0.get("energy_j"), a1.get("energy_j"),
                                          (a1.get("t") or 0) - (a0.get("t") or 0), s["power"]["mean_w"])
            if dj is not None:
                wh, src = dj / 3600, "energy counter"
            elif s["power"]["mean_w"] is not None:
                wh, src = s["power"]["mean_w"] * wall / 3600, "sampled mean W x wall clock"
            else:
                wh, src = None, None
            pd.update({"power": s["power"], "idle_power": power_summary(idle.get(dev, [])),
                       "energy_wh": round(wh, 2) if wh is not None else None, "energy_source": src,
                       "energy_counter_status": why,
                       "wh_per_1000_docs": round(wh / nd * 1000, 2) if wh is not None and nd else None,
                       "utilisation": util_block(sampler, dev, s["util"]),
                       "xpu_smi_peak_used_gb": s["mem_peak_gb"]})
        else:
            pd.update({"power": None, "energy_wh": None, "wh_per_1000_docs": None, "utilisation": None,
                       "note": "not measured: " + ("--no-power" if a.no_power else
                                                   "; ".join(sampler.notes) if sampler else "no xpu-smi id")})
        per_dev[dev] = pd
    row["per_device"] = per_dev
    if have_smi:
        tot = [x["mean_w"] for x in by_dev_power.values()]
        mean_total = round(sum(tot), 2) if tot and all(x is not None for x in tot) else None
        row["power"] = {"mean_w_total": mean_total, **combine_energy(e0, e1, by_dev_power, mean_total, wall, n_read),
                        "source": sampler.mode, "raw_head": sampler.raw_head, "xpu_smi_ids": smi_ids,
                        "notes": sampler.notes[:20],
                        "in_ram_samples_kept_every": sampler.stride, "samples_file": {
                            d: Path(f).name for d, f in sampler.spill_files.items()}}
    else:
        row["power"] = None
    row["memory"] = {"fdinfo_peak_vram_gb_all_workers": fd.peak_total,
                     "fdinfo_peak_vram_gb_by_worker": {str(i): round(v, 2) for i, v in fd.peak_by_worker.items()}
                     or None}

    row["passes"], row["stability"] = score_blocks(blocks, sets, model_id, ",".join(uniq), f"longrun-k{k}",
                                                   t_run0, sampler if have_smi else None)
    row["passes_complete"] = sum(1 for r in row["passes"] if not r["partial"])
    return row


def fit_json(doc: dict, limit: int = MAX_JSON_BYTES) -> str:
    """The results file as JSON, trimmed step by step until it is under limit bytes (counts are never trimmed):
    per-pass power / utilisation / latency detail first, then logs, then the middle passes."""
    def dump():
        return json.dumps(doc, ensure_ascii=False, indent=1)
    txt = dump()
    if len(txt.encode("utf-8")) <= limit:
        return txt
    rows = doc.get("rows") or []
    steps = []
    for r in rows:
        for p in r.get("passes") or []:
            for key in ("util_mean_pct", "power_mean_w", "latency_s", "not_read_reasons"):
                p.pop(key, None)
    steps.append("per-pass power, utilisation and latency detail")
    for cap in (20, 0):
        if len(dump().encode("utf-8")) <= limit:
            break
        for r in rows:
            for key in ("crash_log", "restart_log", "retries", "load_errors"):
                if isinstance(r.get(key), list):
                    r[key] = r[key][:cap]
            if isinstance((r.get("power") or {}).get("raw_head"), dict):
                r["power"]["raw_head"] = {}
        steps.append(f"logs cut to {cap} entries")
    keep = 200
    while len(dump().encode("utf-8")) > limit and keep >= 2:
        for r in rows:
            p = r.get("passes") or []
            if len(p) > keep:
                r["passes_trimmed"] = {"kept_first": keep // 2, "kept_last": keep // 2, "dropped": len(p) - keep}
                r["passes"] = p[:keep // 2] + p[-(keep // 2):]
        steps.append(f"passes cut to first and last {keep // 2}")
        keep //= 2
    doc.setdefault("notes", []).append(f"results file trimmed to stay under {limit} bytes: " + "; ".join(steps))
    return dump()


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


UTIL_DEF = ("GPU utilisation % per card from xpu-smi dump metric 0 (header column whose name holds 'util' and 'gpu' "
            "but not 'mem'; column and raw_header recorded), over the read wall clock: mean, p50 / p95 (1 % bins), "
            "frac_ge_90 = share of 1-second samples at or above 90 %; null with the raw header when the column is "
            "missing")


def main_longrun(a, argv, ks, model_dir: Path, inv: dict, devnames: dict, n: int | None, out_dir: Path,
                 out_path: Path) -> int:
    from eval.bench_tiered import expected_for, subset_inbox
    from ledger_room.pipeline import git_rev, load_inbox

    names = [x.strip() for x in (a.sets or a.set).split(",") if x.strip()]
    bad = [x for x in names if x.startswith("private")]
    if bad:
        print(f"private sets are not used in a long run: {bad}", file=sys.stderr)
        return 2
    sets = []
    for name in names:
        _, docs_all = load_inbox(name)
        inbox = subset_inbox(name, n) if n else name
        _, docs = load_inbox(inbox)
        sets.append({"name": name, "inbox": inbox, "docs": docs, "docs_in_set": len(docs_all),
                     "exp": expected_for(name, {d["doc_id"] for d in docs})})
    if not sum(len(st["docs"]) for st in sets):
        print("no documents in the sets", file=sys.stderr)
        return 2
    if a.repeat is not None and a.repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2

    doc = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "code_rev": git_rev(),
        "mode": "longrun", "dry_run": a.dry_run,
        "command": " ".join(["python -m eval.bench_concurrency"] + (argv or sys.argv[1:])),
        "sets": [{"name": st["name"], "docs_in_set": st["docs_in_set"], "docs_used": len(st["docs"]),
                  "answer_key": f"eval/expected_{st['name']}.json" if st["exp"] is not None else None}
                 for st in sets],
        "docs_per_pass": sum(len(st["docs"]) for st in sets),
        "repeat": a.repeat, "duration_min": a.duration_min, "max_restarts": a.max_restarts,
        "model": model_dir.name, "devices": a.devices, "factory": a.factory,
        "inventory": {k: inv.get(k) for k in ("os", "host", "cpu", "ram_gb", "openvino", "openvino_genai", "devices",
                                               "kernel")},
        "xpu_smi_discovery": None if a.no_power else smi_discovery(a.xpu_smi_cmd),
        "definitions": {
            "pass": "one pass = every set once, in the order given; each (pass, set) is scored on its own "
                    "(pipeline.audit + eval.read_accuracy + eval.run_eval.score, as bench_tiered), so passes of the "
                    "same set should give the same F1, key fields wrong and docs for a person",
            "stop": "--repeat N: N passes. --duration-min M: no new document is handed out once M minutes of read "
                    "wall clock have passed; documents already handed out (and their retries) are finished. The "
                    "last pass of a timed run is usually partial (partial: true): scored on the documents it "
                    "reached, and left out of the stability comparison",
            "docs_per_hour": "documents read / read wall clock of the whole run (all workers loaded -> last document "
                             "back; reloads after crashes included). per_device counts the documents read by the "
                             "workers on that card over the same wall clock",
            "per_hour": "crashes_per_hour, restarts_per_hour = count x 3600 / read wall clock (s). A restart follows "
                        "a crash or a reader error (restarts_after_reader_error)",
            "docs_not_read_reader_failure": "documents sent to a person because the reader failed twice on them "
                                            "(READ_REVIEW, reason reader_crash / reader_error / no_reader)",
            "wh_per_1000_docs": "per card: xpu-smi energy counter (end - start) when it behaves as a running "
                                "counter, else mean sampled power x read wall clock; / documents read on that card "
                                "x 1000",
            "utilisation": UTIL_DEF,
            "samples": "power / memory / utilisation summaries are exact running sums over every sample; the full "
                       "1-second series is in the side CSV (side_files), not in this file; the in-RAM copy used "
                       "for per-pass windows is thinned (in_ram_samples_kept_every)",
            "supervisor": "a worker that dies is restarted and its document retried once; a second failure sends "
                          "it to a person; logs in this file are capped at logs_capped_at entries, counts are not",
        },
        "notes": ([a.note] if a.note else []) + ["OCR runs on the CPU inside each worker, so K workers also share "
                                                  "the CPU"],
        "rows": [],
    }

    def save():
        doc["measured_on_b70"] = (not a.dry_run and bool(doc["rows"]) and
                                  all("B70" in (devnames.get(d) or "") for d in a.devices))
        out_path.write_text(fit_json(doc), encoding="utf-8")

    print(f"long run: sets {names} ({doc['docs_per_pass']} documents a pass), model {model_dir.name}, devices "
          f"{a.devices}, K = {ks}, repeat {a.repeat}, duration {a.duration_min} min", flush=True)
    for i, k in enumerate(ks):
        if i and a.settle_s > 0:
            time.sleep(a.settle_s)
        print(f"\n== K = {k} ==", flush=True)
        try:
            row = run_longrun(k, a, str(model_dir), sets, out_dir)
        except Exception as ex:   # noqa: BLE001
            row = {"k": k, "error": f"{type(ex).__name__}: {ex}"[:500]}
        for dev, pd in (row.get("per_device") or {}).items():
            if not pd["reached_1000_docs"]:
                doc["notes"].append(f"K={k}: {dev} read {pd['docs_read']} documents (< 1,000)")
        for name, stb in (row.get("stability") or {}).items():
            if stb.get("complete_passes", 0) > 1 and not stb["all_complete_passes_same_scoring"]:
                doc["notes"].append(f"K={k}: {name}: scoring differs between complete passes (see passes)")
        doc["rows"].append(row)
        save()
        print(f"  K={k}: {row.get('docs_read')} read in {row.get('wall_min')} min = {row.get('docs_per_hour')}/h, "
              f"crashes/h {row.get('crashes_per_hour')}, restarts/h {row.get('restarts_per_hour')}, not read "
              f"{row.get('docs_not_read_reader_failure')}, passes {row.get('passes_complete')} complete"
              + "".join(f"\n    {d}: {pd['docs_read']} docs, {pd['docs_per_hour']}/h, Wh/1000 "
                        f"{pd.get('wh_per_1000_docs')}, util mean {(pd.get('utilisation') or {}).get('mean_pct')} %"
                        for d, pd in (row.get("per_device") or {}).items())
              + (f"  ERROR {row['error']}" if row.get("error") else ""), flush=True)
    print(f"\nwrote {out_path} ({out_path.stat().st_size} bytes)")
    return 0


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
    ap.add_argument("--max-restarts", type=int, default=None, help="per K run (default 50; long run 1000)")
    ap.add_argument("--xpu-smi-ids", default=None, help="e.g. GPU.0=0,GPU.1=1 (default GPU.N -> N)")
    ap.add_argument("--xpu-smi-cmd", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--no-power", action="store_true")
    ap.add_argument("--idle-s", type=float, default=10, help="idle power/memory baseline before each K (seconds)")
    ap.add_argument("--settle-s", type=float, default=5, help="pause between K runs so device memory is freed")
    ap.add_argument("--factory", default=DEFAULT_FACTORY, help=argparse.SUPPRESS)
    ap.add_argument("--no-inventory", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--out", default=None)
    ap.add_argument("--note", default=None)
    ap.add_argument("--sets", default=None, help="long run: comma list of sets read one after another, e.g. "
                                                 "heldout,hard,blind3,main (private sets refused)")
    ap.add_argument("--repeat", type=int, default=None, help="long run: passes over the sets (with --duration-min: "
                                                             "at most this many)")
    ap.add_argument("--duration-min", type=float, default=None, help="long run: keep cycling through the sets until "
                                                                     "this many minutes of read wall clock have passed")
    a = ap.parse_args(argv)
    longrun = bool(a.sets or a.repeat or a.duration_min)
    if a.max_restarts is None:
        a.max_restarts = 1000 if longrun else 50

    import ledger_room  # noqa: F401  (CI=true before openvino is imported)
    from eval import bench_b70 as B
    from eval.bench_tiered import expected_for, model_present, resolve_model, subset_inbox
    from ledger_room.pipeline import git_rev, load_inbox

    ks = [int(x) for x in (a.k or ("1,2" if a.dry_run else "1,2,3,4")).split(",") if x.strip()]
    a.devices = [x.strip() for x in (a.devices or ("CPU" if a.dry_run else "GPU.0")).split(",") if x.strip()]
    models_root = Path(a.models_root).expanduser() if a.models_root else B.default_models_root()
    n = a.n if a.n is not None else (3 if a.dry_run else None)
    out_dir = isolate_outputs()
    default_out = (("results_longrun_dryrun.json" if a.dry_run else "results_longrun.json") if longrun else
                   ("results_concurrency_dryrun.json" if a.dry_run else "results_concurrency.json"))
    out_path = ROOT / "eval" / (a.out or default_out)

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
    if longrun:
        return main_longrun(a, argv, ks, model_dir, inv, devnames, n, out_dir, out_path)

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
            "utilisation": UTIL_DEF,
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
