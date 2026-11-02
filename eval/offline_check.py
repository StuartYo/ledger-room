"""Offline check: one full live read of the held-out inbox while this process's network connections are watched.

  python -m eval.offline_check                         # all 47 held-out documents, default model and device
  python -m eval.offline_check --model-dir E:\\models\\Qwen3.5-9B-int4-ov --device GPU --n 5

Two independent watchers run during model loading, reading, the rules and the export:
  1. Socket sampling (psutil): every --interval seconds (default 1), every inet connection of this process and its
     children is recorded. Any remote address outside loopback (127.0.0.0/8, ::1) fails the check.
  2. Python audit hook (sys.addaudithook): every socket.connect / socket.sendto and every DNS lookup
     (socket.getaddrinfo / gethostbyname) made from Python code is logged, however short-lived. A non-local target
     fails the check.
Limits, written into the result: sampling can miss a native (C++) connection shorter than the interval; the audit
hook only sees Python-level calls. Together they cover the Python code and any long-lived native connection. The
strongest check is still a run with the network adapter disabled (README "Offline"), which this does not replace.
Writes eval/results_offline.json. Reads go to data/out/bench_b70/ so the evaluation cache is not touched.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOCAL_NAMES = {"localhost", "localhost.localdomain", "", None}


def is_local(host) -> bool:
    if host in LOCAL_NAMES:
        return True
    h = str(host).split("%")[0]
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return h.lower() in LOCAL_NAMES or h.lower().endswith(".localhost")
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


class SocketSampler:
    def __init__(self, interval: float):
        import psutil
        self.psutil = psutil
        self.proc = psutil.Process()
        self.interval = interval
        self.samples = 0
        self.seen: dict[tuple, dict] = {}
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _conns(self):
        procs = [self.proc] + self.proc.children(recursive=True)
        for p in procs:
            try:
                yield p.pid, p.net_connections(kind="inet")
            except (self.psutil.NoSuchProcess, self.psutil.AccessDenied) as e:
                self.errors.append(f"{p.pid}: {type(e).__name__}")

    def sample(self):
        self.samples += 1
        for pid, conns in self._conns():
            for c in conns:
                if not c.raddr:
                    continue          # listening or unconnected socket: nothing is being sent anywhere
                key = (pid, c.laddr.ip, c.laddr.port, c.raddr.ip, c.raddr.port, str(c.type))
                if key not in self.seen:
                    self.seen[key] = {"pid": pid, "local": (f"{c.laddr.ip}:{c.laddr.port}" if is_local(c.laddr.ip)
                                                else f"(this machine's LAN address):{c.laddr.port}"),
                                      "remote": f"{c.raddr.ip}:{c.raddr.port}", "status": c.status,
                                      "type": "tcp" if c.type == socket.SOCK_STREAM else "udp",
                                      "remote_is_local": is_local(c.raddr.ip), "first_seen_sample": self.samples}

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.sample()
            except Exception as e:   # keep sampling; record why one sample failed
                self.errors.append(repr(e)[:200])

    def __enter__(self):
        self.sample()
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=5)
        self.sample()


class PythonNetAudit:
    """sys.addaudithook cannot be removed, so it only records while `active` is set."""
    EVENTS = {"socket.connect", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo", "socket.gethostbyname",
              "socket.gethostbyaddr"}

    def __init__(self):
        self.active = False
        self.events: list[dict] = []
        sys.addaudithook(self._hook)

    def _hook(self, event, args):
        if not self.active or event not in self.EVENTS:
            return
        try:
            if event == "socket.connect" or event in ("socket.sendto", "socket.sendmsg"):
                addr = args[1] if len(args) > 1 else None
                host = addr[0] if isinstance(addr, tuple) and addr else addr
            else:
                host = args[0] if args else None
            if isinstance(host, bytes):
                host = host.decode("ascii", "replace")
            if isinstance(host, str) and host.startswith("/"):
                return                # Unix domain socket path, not a network address
            ev = {"event": event, "host": str(host), "is_local": is_local(host)}
            if not ev["is_local"]:      # where the call came from, so a failure names its source
                import traceback
                frames = traceback.extract_stack()[:-2]
                keep = [fr for fr in frames if "site-packages" in fr.filename or str(ROOT) in fr.filename] or frames
                home = str(Path.home())
                ev["stack"] = [f"{fr.filename.replace(home, '~')}:{fr.lineno} {fr.name}" for fr in keep][-8:]
            self.events.append(ev)
        except Exception:
            self.events.append({"event": event, "host": "?", "is_local": False})


def interfaces_up() -> list[str]:
    import psutil
    up = []
    for name, st in psutil.net_if_stats().items():
        if st.isup and "loopback" not in name.lower() and name != "lo":
            up.append(name)
    return sorted(up)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Offline check during a live read (see module docstring)")
    ap.add_argument("--model-dir", default=None, help="default: LEDGER_MODEL_DIR or the reader's default")
    ap.add_argument("--device", default=None)
    ap.add_argument("--set", default="heldout")
    ap.add_argument("--n", type=int, default=None, help="first N documents only (default: the whole set)")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--out", default="results_offline.json")
    a = ap.parse_args(argv)

    # Both watchers start before anything imports openvino: `import openvino` itself loads openvino.tools.ovc,
    # which starts OpenVINO's telemetry (found 2026-11-02), so watching only from model load would miss it.
    audit = PythonNetAudit()
    audit.active = True
    up = interfaces_up()
    print(f"non-loopback interfaces up: {up or 'none'}", flush=True)
    t0 = time.time()
    with SocketSampler(a.interval) as s:
        from eval.bench_b70 import isolate_outputs, subset_inbox, telemetry_status
        from ledger_room import pipeline
        out_dir = isolate_outputs()
        set_name = a.set
        if a.n:
            if a.set != "heldout":
                ap.error("--n works with the held-out set only")
            set_name = subset_inbox(a.n)
        from ledger_room.reader import DEFAULT_MODEL_DIR, Reader
        reader = Reader(a.model_dir or DEFAULT_MODEL_DIR, a.device)
        print(f"model {reader.model_id} on {reader.device}, load {reader.load_s} s", flush=True)
        run = pipeline.audit(set_name, reader, run_id=f"offline-{datetime.now():%Y%m%d-%H%M%S}", cache=False,
                             progress=lambda i, n, did, r: print(f"  [{i}/{n}] {did}", flush=True),
                             model_id=reader.model_id)
        n_docs = run["summary"]["n_docs"]
        time.sleep(3 * a.interval)   # let background senders (thread pools) finish inside the watched window
    audit.active = False
    dur = round(time.time() - t0, 1)
    conns = list(s.seen.values())
    bad_conns = [c for c in conns if not c["remote_is_local"]]
    bad_py = [e for e in audit.events if not e["is_local"]]
    ok = not bad_conns and not bad_py and s.samples >= 2
    res = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "code_rev": pipeline.git_rev(), "host_os": platform.platform(), "synthetic": True,
        "set": a.set, "docs_read": n_docs, "model": reader.model_id, "device": reader.device,
        "openvino_telemetry": telemetry_status(),
        "phases_watched": "imports (openvino), model load, OCR + model reads (cache off), rules, evidence crops, audit log, CSV export",
        "duration_s": dur, "sample_interval_s": a.interval, "samples": s.samples,
        "non_loopback_interfaces_up": up,
        "connections_seen": conns, "non_local_connections": bad_conns,
        "python_network_calls": audit.events, "non_local_python_calls": bad_py,
        "sampling_errors": s.errors[:20],
        "pass": ok,
        "verdict": ("PASS: no connection or lookup to a non-local address" if ok else
                    "FAIL: see non_local_connections / non_local_python_calls" if (bad_conns or bad_py) else
                    "FAIL: too few samples"),
        "limits": ("Sampling can miss a native connection shorter than the interval; the audit hook sees Python-level "
                   "socket calls only. Network adapters were " + ("up" if up else "down") + " during the run. "
                   "Not a replacement for a run with the network adapter disabled."),
        "outputs": str(out_dir),
    }
    p = ROOT / "eval" / a.out
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{res['verdict']} ({s.samples} samples over {dur} s, {len(conns)} connections seen, "
          f"{len(audit.events)} Python network calls) -> {p}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
