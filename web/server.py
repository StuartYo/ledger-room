"""Ledger Room local web app. Python standard library only; no external assets; works offline.

python -m web.server                 # http://127.0.0.1:8780, this machine only
python -m web.server --host 0.0.0.0 --cert cert.pem --key key.pem
                                     # office LAN over HTTPS: refuses to start until user accounts exist
                                     # (python -m web.users add); use --behind-tls-proxy instead of --cert/--key
                                     # when an HTTPS reverse proxy sits in front

Access control (bookkeepers' safeguard regulation Arts. 15, 17 and 19):
  * binds to 127.0.0.1 unless told otherwise;
  * with accounts in users.json every request except the login page needs a session; roles admin / staff;
  * without accounts it runs as one local user and only on the loopback address;
  * an operation log (who logged in, ran, viewed, exported) is appended with the same HMAC chain as the audit log;
  * personal data in results, exports and logs is masked by default (Art. 17 顯示隱碼, ledger_room/privacy.py); a role
    in privacy.UNMASK_ROLES can unmask one run for UNMASK_S seconds after giving a reason, which is logged;
  * at start-up it warns when the data folder is on a volume without full-disk encryption (never blocks).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import ssl
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # allow `python web/server.py`

from ledger_room import agent as agentlib  # noqa: E402
from ledger_room import corrections, export, keys, pipeline, privacy  # noqa: E402
from ledger_room.pipeline import DATA, OUT, AuditLog  # noqa: E402
from web import users as userdb  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web" / "static"
JOBS: dict[str, dict] = {}
SESSIONS: dict[str, dict] = {}
FAILS: dict[str, list] = {}           # name -> [count incl. attempts in flight, locked_until]
IP_FAILS: dict[str, list] = {}        # client IP -> [count incl. attempts in flight, locked_until]
FAILS_LOCK = threading.Lock()
SESSION_S = 8 * 3600
MAX_FAILS, LOCK_S = 5, 300
MAX_IP_FAILS = 20
MAX_BODY = 10_000
CFG = {"users": {}, "local": True, "host": "127.0.0.1", "port": 8780, "tls": False, "secure": False}
RUN_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
OPLOG: AuditLog | None = None
_reader = None
_reader_lock = threading.Lock()
_agent = None                         # the review agent (27B with tools, ledger_room/agent.py), loaded with the reader
_agent_err: str | None = None
PUBLIC = {"/", "/index.html", "/favicon.ico", "/api/login", "/api/me", "/api/logout"}
ADMIN_ONLY = {"/api/audit-log", "/api/oplog"}
UNMASK: dict[tuple, float] = {}      # (user, session token, run) -> expiry of an unmask grant
UNMASK_S = 600
IMG_EXT = {".jpg", ".jpeg", ".png"}
ACTIVE: dict[str, str] = {}           # inbox -> id of the check running on it (one at a time per inbox)
RUN_LOCK = threading.Lock()
REVIEW_LOGS: dict[str, AuditLog] = {}  # run id -> its review log (decisions on held vouchers, exports)
REVIEW_LOCK = threading.Lock()
OVERRIDE_LOCK = threading.Lock()
RUNNING = {"queued", "loading model", "reading", "agent"}
UI_INBOXES = ROOT / "web" / "inboxes.json"
BOX_CACHE: dict[str, dict] = {}       # run id -> runs/<run>/field_boxes.json (tight field boxes, drawing only)
BOX_LOCK = threading.Lock()


_HW: dict[str, str | None] = {}


def hardware_name() -> str | None:
    """The name of the device the loaded model runs on (OpenVINO FULL_DEVICE_NAME, e.g. an Arc Pro B70); None
    before the model is loaded or when the device does not say. Asked once per device."""
    dev = getattr(_reader, "device", None) if _reader is not None else None
    if not dev or ":" in dev or dev.upper().startswith(("AUTO", "MULTI", "HETERO")):
        return None
    if dev not in _HW:
        try:
            import openvino as ov
            _HW[dev] = str(ov.Core().get_property(dev, "FULL_DEVICE_NAME")) or None
        except Exception:
            _HW[dev] = None
    return _HW[dev]


def get_reader():
    global _reader
    with _reader_lock:
        if _reader is None:
            from ledger_room.reader import Reader
            _reader = Reader()
    return _reader


def get_agent():
    """The agent model, loaded on the first live check when this machine has one (agent.availability()); None when
    it has none or it failed to load (the check then runs without it and says so)."""
    global _agent, _agent_err
    with _reader_lock:
        if _agent is None and _agent_err is None and agentlib.availability()["available"]:
            try:
                _agent = agentlib.Agent()
            except Exception as e:      # e.g. not enough card memory: the check goes on without the agent
                _agent_err = f"{type(e).__name__}: {e}"[:300]
    return _agent


def agent_status() -> dict:
    """For /api/status: is the agent on this machine, loaded, where, and how much card memory this process holds."""
    av = agentlib.availability()
    out = {"available": av["available"] and _agent_err is None, "loaded": _agent is not None,
           "model": _agent.model_id if _agent is not None else av["model"],
           "device": _agent.device if _agent is not None else av["device"],
           "reason": "load_failed" if _agent_err else av["reason"], "error": _agent_err}
    if _agent is not None or _reader is not None:
        out["memory_gb"] = agentlib.card_memory_gb() or None
        out["load_s"] = getattr(_agent, "load_s", None)
    return out


def oplog(event: str, **kw) -> None:
    if OPLOG is not None:
        OPLOG.add(event, **kw)


def valid_run(run: str, allow_oplog: bool = False) -> bool:
    if allow_oplog and run == "oplog":
        return True
    return bool(RUN_RE.match(run or "")) and ".." not in run and run != "."


def safe_subpath(sub: str) -> bool:
    """A request sub-path under /static/, /img/ or /out/: plain '/'-separated names only (no empty, '.' or '..'
    segment, no backslash, colon or NUL, nothing that Windows would read as a drive or a UNC share)."""
    if not sub or sub.startswith("/") or any(c in sub for c in "\\:\0"):
        return False
    return all(seg not in ("", ".", "..") for seg in sub.split("/"))


def lexically_inside(p: Path, base: Path) -> bool:
    """Containment by string only: no filesystem access (resolve() on a UNC path would open it)."""
    try:
        a = os.path.normcase(os.path.normpath(str(p)))
        b = os.path.normcase(os.path.normpath(str(base)))
        return os.path.isabs(a) and os.path.commonpath([a, b]) == b
    except ValueError:      # different drives, or a UNC path against a drive path
        return False


def _hval(v) -> str:
    return str(v).replace("\r", "").replace("\n", "")


def attachment(name: str) -> str:
    return 'attachment; filename="' + _hval(name).replace('"', "") + '"'


def ui_inbox_names() -> dict[str, dict] | None:
    """{set: {"zh", "en"}} from web/inboxes.json, in display order; None lists every inbox (--all-inboxes)."""
    if CFG.get("all_inboxes"):
        return None
    try:
        return json.loads(UI_INBOXES.read_text(encoding="utf-8"))["inboxes"]
    except (OSError, ValueError, KeyError):
        return {}


def inboxes() -> list[dict]:
    names = ui_inbox_names()
    out = []
    for p in sorted(DATA.iterdir()) if DATA.exists() else []:
        cj = p / "clients.json"
        if p.name == "out" or not cj.exists() or (names is not None and p.name not in names):
            continue
        cfg = json.loads(cj.read_text(encoding="utf-8"))
        _, docs = pipeline.load_inbox(p.name)
        nm = (names or {}).get(p.name) or {"zh": p.name, "en": p.name}
        out.append({"set": p.name, "n_docs": len(docs), "n_clients": len(cfg["clients"]),
                    "layout_family": cfg.get("layout_family"), "synthetic": bool(cfg.get("synthetic")),
                    "name": {"zh": nm.get("zh") or p.name, "en": nm.get("en") or p.name}})
    order = list(names or {})
    return sorted(out, key=lambda b: order.index(b["set"]) if b["set"] in order else len(order))


def server_utc_offset_s():
    """Seconds the server's local clock is ahead of UTC (run ids are named in local time)."""
    import datetime
    return int(datetime.datetime.now().astimezone().utcoffset().total_seconds())


def public_job(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_") and k != "trace"}


def save_job(job: dict) -> None:
    """runs/<run id>/job.json: where a check got to, written after every document, so a reload, a dropped connection
    or a server restart can show it and resume it (the documents already read come from the cache)."""
    rid = job.get("run_id")
    if not rid:
        return
    p = OUT / "runs" / rid / "job.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"job.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(public_job(job) | {"done": job.get("_done", [])}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass     # progress on disk is a convenience; the check itself goes on


def interrupted_runs(set_name: str) -> list[dict]:
    """Checks on this inbox that stopped before the end (server stopped or crashed): job.json, no result.json."""
    rd = OUT / "runs"
    live_ids = {j.get("run_id") for j in JOBS.values() if j.get("status") in RUNNING}
    out = []
    for p in sorted(rd.glob(f"{set_name}-*/job.json"), reverse=True) if rd.exists() else []:
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (j.get("set") != set_name or j.get("status") not in RUNNING or j.get("run_id") in live_ids
                or (p.parent / "result.json").exists()):
            continue
        out.append({k: j.get(k) for k in ("run_id", "i", "n", "user", "started", "live")}
                   | {"n_done": len(j.get("done") or [])})
    return out[:3]


def run_job(job_id: str, set_name: str, live: bool, user: str, reuse: set[str] | None = None,
            resumed_from: str | None = None):
    job = JOBS[job_id]
    secs: list[float] = []
    try:
        reader = agent = None
        if live:
            job["status"] = "loading model"
            save_job(job)
            reader = get_reader()
            job["model"], job["device"] = reader.model_id, reader.device
            agent = get_agent()
            job["agent"] = {"model": agent.model_id, "device": agent.device} if agent is not None else None
        job["status"] = "reading"
        save_job(job)

        def prog(i, n, doc_id, res):
            t = res["timing"].get("total_s")
            if t and not res.get("cached"):
                secs.append(t)
            job.update(i=i, n=n, doc_id=doc_id, last_s=t, cached=res.get("cached", False),
                       avg_s=round(sum(secs[-10:]) / len(secs[-10:]), 2) if secs else None,
                       n_failed=job.get("n_failed", 0) + bool(res.get("read_failed")))
            job["_done"].append(doc_id)
            save_job(job)
            if not live:
                time.sleep(0.12)   # replay pacing so the progress is visible; reads come from cache

        ag_t: list[float] = []

        def agent_prog(i, n, doc_id, _rec):
            # the agent's turn on the held vouchers: its own counter, so the page does not sit at "50 of 50"
            ag_t.append(time.time())
            per = round((ag_t[-1] - ag_t[0]) / i, 1) if i and len(ag_t) > 1 else None
            job.update(status="agent", agent_i=i, agent_n=n, agent_avg_s=per,
                       **({"doc_id": doc_id} if doc_id else {}))
            save_job(job)

        from ledger_room.registry import load_default
        res = pipeline.audit(set_name, reader, cache=not live, progress=prog, registry=load_default(), operator=user,
                             platform="auto", run_id=job["run_id"], reuse=reuse, resumed_from=resumed_from,
                             on_read_error="hold", agent=agent, agent_progress=agent_prog,
                             # a replay (no model loaded) shows the agent runs stored for these documents, checked again
                             # by code; a live check without an agent model has no agent step, and says so
                             agent_replay=not live)
        job.update(status="done", summary=res["summary"], carried=carry_over_decisions(set_name, job["run_id"], res))
        save_job(job)
        oplog("run_done", user=user, run_id=job["run_id"], n_exceptions=res["summary"]["n_exceptions"])
    except Exception as e:  # surface the error in the UI
        job.update(status="error", error=f"{e}", trace=traceback.format_exc()[-2000:])
        save_job(job)
        oplog("run_failed", user=user, run_id=job.get("run_id"), error=f"{type(e).__name__}: {e}"[:300])
    finally:
        with RUN_LOCK:
            if ACTIVE.get(set_name) == job_id:
                ACTIVE.pop(set_name, None)


def review_log(run: str) -> AuditLog:
    with REVIEW_LOCK:
        lg = REVIEW_LOGS.get(run)
        if lg is None:
            lg = REVIEW_LOGS[run] = AuditLog(OUT / "runs" / run / "review.jsonl", append=True)
        return lg


def carry_over_decisions(set_name: str, run_id: str, result: dict) -> int:
    """A new check of the same inbox keeps the reviewers' decisions from the last check that has any, for each voucher
    whose issues are the same rules as before (a voucher with a new or a different issue is decided again). Each
    carried decision is a new entry in this run's review log, naming the earlier run and who decided."""
    rd = OUT / "runs"
    prev = None
    for p in sorted(rd.glob(f"{set_name}-*/review.jsonl"), reverse=True):
        if p.parent.name != run_id and (p.parent / "result.json").exists() and review_state(p.parent.name)[0]:
            prev = p.parent.name
            break
    if prev is None:
        return 0
    old_dec, _ = review_state(prev)
    try:
        old = json.loads((rd / prev / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0

    def rules_by_doc(r: dict) -> dict[str, frozenset]:
        out: dict[str, set] = {}
        for e in r.get("exceptions", []) + (r.get("period_check") or {}).get("exceptions", []):
            out.setdefault(e.get("doc_id"), set()).add(e.get("rule_id"))
        return {k: frozenset(v) for k, v in out.items()}

    before, now = rules_by_doc(old), rules_by_doc(result)
    n = 0
    for doc, d in old_dec.items():
        if doc in now and now[doc] == before.get(doc):
            review_log(run_id).add("review", doc_id=doc, decision=d["decision"], note=d.get("note") or "",
                                   user=d.get("user"), role="carried over", carried_from=prev, decided_at=d.get("ts"))
            n += 1
    if n:
        oplog("review_carried_over", run_id=run_id, from_run=prev, n=n)
    return n


def review_state(run: str) -> tuple[dict[str, dict], list[dict]]:
    """The latest decision per document (an "undo" removes it) and the exports, from runs/<run>/review.jsonl."""
    p = OUT / "runs" / run / "review.jsonl"
    dec: dict[str, dict] = {}
    exports: list[dict] = []
    for line in AuditLog.lines(p) if p.exists() else []:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "review" and e.get("doc_id"):
            if e.get("decision") == "undo":
                dec.pop(e["doc_id"], None)
            else:
                dec[e["doc_id"]] = {k: e.get(k) for k in ("decision", "user", "note", "ts")}
        elif e.get("event") == "export":
            exports.append({k: e.get(k) for k in ("user", "ts", "counts")})
    return dec, exports


def review_entries(run: str) -> list[dict]:
    p = OUT / "runs" / run / "review.jsonl"
    out = []
    for line in AuditLog.lines(p) if p.exists() else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def overrides_of(run: str) -> dict[str, dict]:
    """Documents whose agent fixes a person rejected on this run (ledger_room/agent.py overrides_state)."""
    return agentlib.overrides_state(review_entries(run))


def base_result(result: dict, run: str) -> dict:
    """result.json with the agent fixes a person rejected taken out again (before any field correction)."""
    return agentlib.apply_overrides(result, overrides_of(run))


def corrections_of(run: str) -> dict[str, dict[str, dict]]:
    """Field corrections made by reviewers on this run (corrections.py), from its review log."""
    return corrections.read_state(OUT / "runs" / run / "review.jsonl")


_EFF: dict[str, tuple] = {}   # run -> ((review log size, mtime, result summary), corrected result)


def effective_result(result: dict, run: str) -> dict:
    """result.json with the reviewers' field corrections applied and the rules checked again (corrections.apply).
    The last answer per run is kept until the review log changes (a check of the rules takes a few tenths of a second)."""
    lp = OUT / "runs" / run / "review.jsonl"
    try:
        st = lp.stat()
        sig = (st.st_size, st.st_mtime_ns, json.dumps(result.get("summary"), sort_keys=True, default=str)[:2000])
    except OSError:
        return result
    hit = _EFF.get(run)
    if hit and hit[0] == sig:
        return hit[1]
    out = corrections.apply(base_result(result, run), corrections_of(run))
    _EFF[run] = (sig, out)
    return out


class H(BaseHTTPRequestHandler):
    timeout = 30        # a client that stops sending cannot hold a thread forever

    def log_message(self, *a):
        pass

    # ---- responses ----
    def _headers(self, ctype: str, n: int, code: int = 200, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(n))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; "
                                                    "style-src 'self' 'unsafe-inline'; connect-src 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, _hval(v))
        self.end_headers()

    def _json(self, obj, code=200, extra: dict | None = None):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._headers("application/json; charset=utf-8", len(b), code, extra)
        self.wfile.write(b)

    def _file(self, p: Path, base: Path, download: str | None = None):
        p = self._inside(p, base)
        if p is None:
            return self._json({"error": "not found"}, 404)
        b = p.read_bytes()
        extra = {"Content-Disposition": attachment(download)} if download else None
        self._headers(mimetypes.guess_type(p.name)[0] or "application/octet-stream", len(b), 200, extra)
        self.wfile.write(b)

    # ---- sessions ----
    def _ip(self) -> str:
        return self.client_address[0]

    def _session(self) -> dict | None:
        if CFG["local"]:
            return {"user": "local", "role": "admin"}
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "lr_session":
                s = SESSIONS.get(v)
                if s and s["exp"] > time.time():
                    s["token"] = v
                    return s
                SESSIONS.pop(v, None)
        return None

    def _unmasked(self, s: dict, run: str) -> bool:
        exp = UNMASK.get((s["user"], s.get("token", "local"), run))
        return bool(exp and exp > time.time() and privacy.can_unmask(s.get("role")))

    @staticmethod
    def _inside(p: Path, base: Path) -> Path | None:
        if not lexically_inside(p, base):     # before any filesystem call
            return None
        try:
            p = p.resolve()
            p.relative_to(base.resolve())
        except Exception:
            return None
        return p if p.is_file() else None

    def _host_ok(self) -> bool:
        """Local mode trusts every request, so it must come to this machine by a loopback name (DNS rebinding)."""
        if not CFG["local"]:
            return True
        port = CFG["port"]
        host = (self.headers.get("Host") or "").strip().lower()
        if host and host in CFG.get("proxy_hosts", ()):
            return True     # --proxy-origin / --proxy-host: an authenticating reverse proxy on this machine
        return host in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None:
            scheme = "https" if CFG["tls"] or CFG["secure"] else "http"
            if origin != f"{scheme}://{self.headers.get('Host') or ''}" and origin != CFG.get("proxy_origin"):
                return False
        site = self.headers.get("Sec-Fetch-Site")
        return site is None or site in ("same-origin", "none")

    def _gate(self, path: str):
        """None when allowed; otherwise an error response has been sent."""
        if path in PUBLIC or path.startswith("/static/"):
            return None, None
        s = self._session()
        if s is None:
            self._json({"error": "login required"}, 401)
            return False, None
        if path in ADMIN_ONLY and s["role"] != "admin":
            oplog("denied", user=s["user"], path=path, ip=self._ip())
            self._json({"error": "admin only"}, 403)
            return False, s
        return None, s

    def _body(self) -> dict | None:
        """The JSON object sent, or None after an error response (bad length, too long, not a JSON object)."""
        raw = (self.headers.get("Content-Length") or "0").strip()
        if not raw.isdigit():
            self.close_connection = True
            self._json({"error": "bad Content-Length"}, 400)
            return None
        n = int(raw)
        if n > MAX_BODY:
            self.close_connection = True
            self._json({"error": "request too large"}, 413)
            return None
        try:
            b = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            b = None
        if not isinstance(b, dict):
            self._json({"error": "send a JSON object"}, 400)
            return None
        return b

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "unexpected Host header"}, 403)
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = unquote(u.path)
        bad, s = self._gate(path)
        if bad is False:
            return
        for pre in ("/static/", "/img/", "/out/"):
            if path.startswith(pre) and not safe_subpath(path[len(pre):]):
                return self._json({"error": "not found"}, 404)
        if path in ("/api/result", "/api/export", "/api/audit-log", "/api/review", "/api/export-summary", "/api/boxes") \
                and not valid_run(q.get("run", "")):
            return self._json({"error": "bad run"}, 400)
        if path in ("/", "/index.html"):
            return self._file(STATIC / "index.html", STATIC)
        if path == "/favicon.ico":      # browsers ask for it at the root whatever the page links
            return self._file(STATIC / "brand" / "ledger-room.ico", STATIC)
        if path.startswith("/static/"):
            return self._file(STATIC / path[len("/static/"):], STATIC)
        if path == "/api/me":
            me = self._session()
            tz = server_utc_offset_s()   # run ids carry the server's local time; the page needs this to show "x min ago"
            return self._json({"user": me["user"], "role": me["role"], "local": CFG["local"], "tz_offset_s": tz,
                               "can_unmask": privacy.can_unmask(me["role"])} if me else
                              {"user": None, "local": CFG["local"], "tz_offset_s": tz})
        if path.startswith("/img/"):    # receipt images only: not clients.json, not data/out (results, logs, cache)
            p = DATA / path[len("/img/"):]
            if p.suffix.lower() not in IMG_EXT or self._inside(p, OUT) is not None:
                return self._json({"error": "not found"}, 404)
            return self._file(p, DATA)
        if path.startswith("/out/"):    # evidence crops only; results, exports and logs go through the API below
            p = OUT / path[len("/out/"):]
            if p.suffix.lower() not in IMG_EXT or p.parent.name != "crops":
                return self._json({"error": "not found"}, 404)
            return self._file(p, OUT)
        if path == "/api/inboxes":
            return self._json(inboxes())
        if path == "/api/inbox":
            st = q.get("set", "main")
            if st not in {i["set"] for i in inboxes()}:
                return self._json({"error": "unknown inbox"}, 400)
            clients, docs = pipeline.load_inbox(st)
            box = next(i for i in inboxes() if i["set"] == st)
            return self._json({"set": st, "name": box["name"], "synthetic": box["synthetic"],
                               "skipped": pipeline.skipped_files(st),
                               "clients": [privacy.mask_client(c) for c in clients.values()],
                               "docs": [{k: v for k, v in d.items() if k != "path"} | {"image": f"{st}/clients/{d['doc_id']}"}
                                        for d in docs]})
        if path == "/api/runs":
            rd = OUT / "runs"
            runs = sorted([p.name for p in rd.iterdir() if (p / "result.json").exists()], reverse=True) if rd.exists() else []
            return self._json(runs)
        if path == "/api/result":
            run = q.get("run", "")
            clear = q.get("unmask") == "1" and self._unmasked(s, run)
            oplog("view_result", user=s["user"], run_id=run, ip=self._ip(), masked=not clear)
            p = self._inside(OUT / "runs" / run / "result.json", OUT / "runs")
            if p is None:
                return self._json({"error": "not found"}, 404)
            res = effective_result(json.loads(p.read_text(encoding="utf-8")), run)
            return self._json(res if clear else privacy.mask_result(res))
        if path == "/api/review":
            run = q.get("run", "")
            rp = self._inside(OUT / "runs" / run / "result.json", OUT / "runs")
            if rp is None:
                return self._json({"error": "not found"}, 404)
            result = effective_result(json.loads(rp.read_text(encoding="utf-8")), run)
            dec, exports = review_state(run)
            try:
                status = {r["doc_id"]: r["status"] for r in export.rows(result)}
            except (KeyError, TypeError, ValueError):
                status = {}
            if not (q.get("unmask") == "1" and self._unmasked(s, run)):
                names = privacy.personal_names(result) if "docs" in result else ()
                dec = {k: privacy.mask_entries([v], names)[0] for k, v in dec.items()}
            lp = OUT / "runs" / run / "review.jsonl"
            v = AuditLog.verify_detail(lp, keys.audit_key(create=False), head=True) if lp.exists() else None
            return self._json({"decisions": dec, "exports": exports, "status": status, "corrections": corrections_of(run),
                               "agent_overrides": overrides_of(run),
                               "blocking": sorted(export.BLOCKING),
                               "log_ok": None if v is None else bool(v["chain_ok"] and v["mac_ok"] is not False
                                                                      and v.get("tail_ok") is not False)})
        if path == "/api/boxes":
            return self._boxes(q.get("run", ""), q.get("doc", ""))
        if path == "/api/export-summary":
            run = q.get("run", "")
            rdir = OUT / "runs" / run
            rp = self._inside(rdir / "result.json", OUT / "runs")
            if rp is None:
                return self._json({"error": "not found"}, 404)
            corr = corrections_of(run)
            oplog("export_summary_csv", user=s["user"], run_id=run, ip=self._ip())
            if corr or overrides_of(run):    # corrected amounts change the totals: built from the corrected result
                tmp = rdir / f"export_summary.{threading.get_ident()}.tmp"
                export.write_summary_csv(effective_result(json.loads(rp.read_text(encoding="utf-8")), run), tmp)
                b = tmp.read_bytes()
                tmp.unlink(missing_ok=True)
                self._headers("text/csv; charset=utf-8", len(b), 200,
                              {"Content-Disposition": attachment(f"ledger_room_{run}_401_summary.csv")})
                self.wfile.write(b)
                return
            if not (rdir / "export_summary.csv").exists():
                export.write_summary_csv(json.loads(rp.read_text(encoding="utf-8")), rdir / "export_summary.csv")
            return self._file(rdir / "export_summary.csv", OUT / "runs", download=f"ledger_room_{run}_401_summary.csv")
        if path == "/api/jobs":
            st = q.get("set", "")
            if st not in {i["set"] for i in inboxes()}:
                return self._json({"error": "unknown inbox"}, 400)
            jid = ACTIVE.get(st)
            j = JOBS.get(jid) if jid else None
            return self._json({"active": public_job(j) if j and j.get("status") in RUNNING else None,
                               "interrupted": interrupted_runs(st)})
        if path == "/api/export":
            run = q.get("run", "")
            clear = q.get("unmask") == "1" and self._unmasked(s, run)
            oplog("export_media_csv", user=s["user"], run_id=run, ip=self._ip(), masked=not clear)
            rp = self._inside(OUT / "runs" / run / "result.json", OUT / "runs")
            result = json.loads(rp.read_text(encoding="utf-8")) if rp is not None else {}
            if "docs" in result and "clients" in result:
                result = effective_result(result, run)     # the reviewers' field corrections, rules checked again
                # the reviewers' decisions (review.jsonl) applied; the export itself goes into that log too
                dec, _ = review_state(run)
                review = {k: {"decision": v["decision"], "user": v["user"], "note": v.get("note") or ""}
                          for k, v in dec.items()}
                text = export.reviewed_csv_text(result, review)
                counts: dict[str, int] = {}
                for r in export.rows(result, review):
                    counts[r["status"]] = counts.get(r["status"], 0) + 1
                review_log(run).add("export", user=s["user"], counts=counts, reviewed=len(review), masked=not clear,
                                    corrected_fields=result.get("summary", {}).get("n_corrected_fields", 0))
                if not clear:
                    text = privacy.mask_csv(text, privacy.personal_names(result))
                b = text.encode("utf-8")
                name = f"ledger_room_{run}_media_reviewed{'' if clear else '_masked'}.csv"
                self._headers("text/csv; charset=utf-8", len(b), 200, {"Content-Disposition": attachment(name)})
                self.wfile.write(b)
                return
            name = f"ledger_room_{run}_media{'' if clear else '_masked'}.csv"
            rdir = OUT / "runs" / run
            if (valid_run(run) and lexically_inside(rdir, OUT / "runs") and (rdir / "result.json").is_file()
                    and not (rdir / "export_media.csv").exists()):
                # runs from before the CSV was written at the end of each run: build it from the stored result
                export.write_csv(json.loads((rdir / "result.json").read_text(encoding="utf-8")), rdir / "export_media.csv")
            p = self._inside(OUT / "runs" / run / "export_media.csv", OUT / "runs")
            if p is None or clear:
                return self._file(OUT / "runs" / run / "export_media.csv", OUT / "runs", download=name)
            rp = p.parent / "result.json"
            if not rp.is_file():     # without the result we do not know which names to mask
                return self._json({"error": "not found"}, 404)
            names = privacy.personal_names(json.loads(rp.read_text(encoding="utf-8")))
            b = privacy.mask_csv(p.read_text(encoding="utf-8"), names).encode("utf-8")
            self._headers("text/csv; charset=utf-8", len(b), 200, {"Content-Disposition": attachment(name)})
            self.wfile.write(b)
            return
        if path in ("/api/audit-log", "/api/oplog"):
            if path == "/api/oplog":
                p = OUT / "oplog.jsonl"
            else:
                p = OUT / "runs" / q.get("run", "") / "audit_log.jsonl"
                if not lexically_inside(p, OUT / "runs"):
                    return self._json({"error": "bad run"}, 400)
            if not p.exists():
                return self._json({"error": "not found"}, 404)
            oplog("view_" + path.rsplit("/", 1)[-1].replace("-", "_"), user=s["user"], run_id=q.get("run"), ip=self._ip())
            v = AuditLog.verify_detail(p, keys.audit_key(create=False), head=True)
            entries = []
            for x in AuditLog.lines(p):
                try:
                    entries.append(json.loads(x))
                except ValueError:
                    entries.append({"event": "unreadable_line"})
            if not (q.get("unmask") == "1" and self._unmasked(s, q.get("run") or "oplog")):
                entries = privacy.mask_entries(entries)
            return self._json({"verified": v["chain_ok"] and v["mac_ok"] is not False and v.get("tail_ok") is not False
                               and not v.get("unsigned"),
                               "chain_ok": v["chain_ok"], "mac_ok": v["mac_ok"], "tail_ok": v.get("tail_ok"),
                               "unsigned": bool(v.get("unsigned")),
                               "entries_verified": v["n"], "entries": entries[-500:]})
        if path.startswith("/api/job/"):
            j = JOBS.get(path.rsplit("/", 1)[-1])
            return self._json(public_job(j) if j else {"error": "no such job"}, 200 if j else 404)
        if path == "/api/status":
            return self._json({"model_loaded": _reader is not None,
                               "model": _reader.model_id if _reader else None,
                               "device": _reader.device if _reader else None,
                               "hardware": hardware_name(),
                               "strict": pipeline.STRICT,
                               "agent": agent_status()})
        return self._json({"error": "not found"}, 404)

    def _login(self, b: dict):
        name, pw = str(b.get("user", ""))[:64], str(b.get("password", ""))[:256]
        if CFG["local"]:
            return self._json({"user": "local", "role": "admin", "local": True})
        ip, now = self._ip(), time.time()
        with FAILS_LOCK:        # reserve the attempt before the slow password check, so parallel guesses count too
            f, g = FAILS.setdefault(name, [0, 0]), IP_FAILS.setdefault(ip, [0, 0])
            locked = f[1] > now or g[1] > now or f[0] >= MAX_FAILS or g[0] >= MAX_IP_FAILS
            if not locked:
                f[0] += 1
                g[0] += 1
        if locked:
            oplog("login_locked", user=name, ip=ip)
            return self._json({"error": "too many attempts; try again in a few minutes"}, 429)
        role = userdb.check(CFG["users"], name, pw)
        with FAILS_LOCK:
            if role is None:
                for d, k, cap in ((FAILS, name, MAX_FAILS), (IP_FAILS, ip, MAX_IP_FAILS)):
                    e = d.setdefault(k, [0, 0])
                    if e[0] >= cap:
                        d[k] = [0, time.time() + LOCK_S]
            else:
                FAILS.pop(name, None)
                g = IP_FAILS.get(ip)
                if g:
                    g[0] = max(0, g[0] - 1)
        if role is None:
            oplog("login_failed", user=name, ip=ip)
            return self._json({"error": "wrong user name or password"}, 401)
        tok = secrets.token_urlsafe(32)
        SESSIONS[tok] = {"user": name, "role": role, "exp": time.time() + SESSION_S}
        oplog("login", user=name, role=role, ip=ip)
        secure = "; Secure" if CFG["tls"] or CFG["secure"] else ""
        return self._json({"user": name, "role": role, "local": False},
                          extra={"Set-Cookie": f"lr_session={tok}; HttpOnly; SameSite=Strict; Path=/; "
                                               f"Max-Age={SESSION_S}{secure}"})

    def _boxes(self, run: str, doc: str):
        """Field boxes around the value only, turned with the paper (ledger_room/tight_boxes.py), for one document.
        Drawing only; kept in runs/<run>/field_boxes.json so each is worked out once."""
        rp = self._inside(OUT / "runs" / run / "result.json", OUT / "runs")
        if rp is None:
            return self._json({"error": "not found"}, 404)
        res = effective_result(json.loads(rp.read_text(encoding="utf-8")), run)
        d = next((x for x in res.get("docs", []) if x.get("doc_id") == doc), None)
        img = DATA / d["image"] if d and d.get("image") else None
        if img is None or img.suffix.lower() not in IMG_EXT or self._inside(img, DATA) is None \
                or self._inside(img, OUT) is not None:
            return self._json({"error": "not found"}, 404)
        from ledger_room import tight_boxes
        with BOX_LOCK:
            data = BOX_CACHE.get(run)
            if data is None:
                data = BOX_CACHE[run] = tight_boxes.load_cache(OUT / "runs" / run)
            cache = data["docs"].setdefault(doc, {})
            n0 = len(cache)
        try:
            boxes = tight_boxes.for_doc(img, d, cache)
        except Exception as e:      # drawing only: the page keeps the reader's boxes
            return self._json({"boxes": {}, "error": f"{type(e).__name__}"})
        if len(cache) != n0:
            with BOX_LOCK:
                tight_boxes.save_cache(OUT / "runs" / run, data)
        return self._json({"boxes": boxes, "image_size": d.get("image_size")})

    def _correct(self, body: dict, s: dict):
        """A reviewer corrects one field the reader got wrong: {run, doc_id, field, value, note?} or {..., revert: true}.
        Logged in the run's review log (hash chain + HMAC, like the decisions) and the operation log; the machine
        reading stays in result.json. Answers with the corrected document and its exceptions after the rules ran again."""
        run, doc, field = str(body.get("run", "")), str(body.get("doc_id", ""))[:200], str(body.get("field", ""))
        note = str(body.get("note", "")).strip()[:300]
        if not valid_run(run):
            return self._json({"error": "bad run"}, 400)
        rp = self._inside(OUT / "runs" / run / "result.json", OUT / "runs")
        if rp is None:
            return self._json({"error": "unknown run"}, 400)
        stored = json.loads(rp.read_text(encoding="utf-8"))
        result = base_result(stored, run)
        d0 = next((d for d in result.get("docs", []) if d.get("doc_id") == doc), None)
        if d0 is None or not isinstance(d0.get("fields"), dict):
            return self._json({"error": "unknown document"}, 400)
        if field not in corrections.EDITABLE:
            return self._json({"error": "this field cannot be corrected", "code": "field"}, 400)
        old = d0["fields"].get(field)
        try:
            new = old if body.get("revert") is True else corrections.normalize(field, body.get("value"))
        except corrections.Invalid as e:
            return self._json({"error": str(e), "code": e.code}, 400)
        with REVIEW_LOCK:       # one correction at a time per server: "previous" must be the value shown before it
            cur = corrections_of(run).get(doc, {}).get(field)
            previous = cur["new"] if cur else old
            changed = new != previous
        ag = d0.get("agent") or {}
        over_agent = (field in (ag.get("accepted") or {}) and ag.get("status") == "verified"
                      and not ag.get("overridden"))
        if changed:
            # a person changing a value the agent put in overrides the agent: the entry says so
            review_log(run).add("correct", doc_id=doc, field=field, old=old, new=new, previous=previous, note=note,
                                user=s["user"], role=s["role"],
                                **({"over_agent": True, "machine": (ag.get("machine") or {}).get(field)}
                                   if over_agent else {}))
            oplog("correct", user=s["user"], run_id=run, doc_id=doc, field=field, old=previous, new=new, ip=self._ip())
        eff = effective_result(stored, run)
        d = next(x for x in eff["docs"] if x["doc_id"] == doc)
        exs = [e for e in eff["exceptions"] if e.get("doc_id") == doc]
        try:
            status = next((r["status"] for r in export.rows(eff) if r["doc_id"] == doc), None)
        except (KeyError, TypeError, ValueError):
            status = None
        out = {"ok": True, "changed": changed, "doc_id": doc, "field": field, "value": d["fields"].get(field),
               "machine": old, "corrections": d.get("corrections") or {}, "exceptions": exs, "status": status}
        if not self._unmasked(s, run):
            names = privacy.personal_names(result)
            out["exceptions"] = privacy.mask_entries(exs, names)
        return self._json(out)

    def _agent_override(self, body: dict, s: dict):
        """A person does not accept the agent's fixes on one document ({run, doc_id, action: "reject", note?}), or
        takes that back ("restore"). Logged in the run's review log (hash chain + HMAC) and the operation log. A rejected
        document is back to its first reading and its original reasons: it waits for a person again."""
        run, doc = str(body.get("run", "")), str(body.get("doc_id", ""))[:200]
        action, note = str(body.get("action", "")), str(body.get("note", "")).strip()[:300]
        if not valid_run(run):
            return self._json({"error": "bad run"}, 400)
        rp = self._inside(OUT / "runs" / run / "result.json", OUT / "runs")
        if rp is None:
            return self._json({"error": "unknown run"}, 400)
        if action not in ("reject", "restore"):
            return self._json({"error": "action must be reject or restore"}, 400)
        stored = json.loads(rp.read_text(encoding="utf-8"))
        d0 = next((d for d in stored.get("docs", []) if d.get("doc_id") == doc), None)
        if d0 is None or (d0.get("agent") or {}).get("status") != "verified":
            return self._json({"error": "no agent fix on this document", "code": "no_agent"}, 400)
        with OVERRIDE_LOCK:     # one at a time: "changed" must compare with the state just before this entry
            now = doc in overrides_of(run)
            changed = now != (action == "reject")
            if changed:
                review_log(run).add("agent_override", doc_id=doc, action=action, note=note, user=s["user"],
                                    role=s["role"], accepted=d0["agent"].get("accepted"),
                                    machine=d0["agent"].get("machine"))
        if changed:
            oplog("agent_override", user=s["user"], run_id=run, doc_id=doc, action=action, ip=self._ip())
        eff = effective_result(stored, run)
        exs = [e for e in eff["exceptions"] if e.get("doc_id") == doc]
        if not self._unmasked(s, run):
            exs = privacy.mask_entries(exs, privacy.personal_names(stored))
        return self._json({"ok": True, "changed": changed, "doc_id": doc, "action": action, "exceptions": exs})

    def _drain(self) -> None:
        """Read and drop a refused request's body (closing with unread data makes Windows reset the connection
        before the client sees the error)."""
        raw = (self.headers.get("Content-Length") or "0").strip()
        if raw.isdigit() and int(raw) <= MAX_BODY:
            self.rfile.read(int(raw))
        else:
            self.close_connection = True

    def do_POST(self):
        if not self._host_ok():
            self._drain()
            return self._json({"error": "unexpected Host header"}, 403)
        u = urlparse(self.path)
        path = u.path
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":     # a cross-site form or no-cors fetch cannot send this without a preflight
            self._drain()
            return self._json({"error": "send application/json"}, 415)
        if not self._same_origin():
            self._drain()
            return self._json({"error": "cross-origin request refused"}, 403)
        body = self._body()
        if body is None:
            return
        if path == "/api/login":
            return self._login(body)
        if path == "/api/logout":
            s = self._session()
            if s and s.get("token"):
                SESSIONS.pop(s["token"], None)
                oplog("logout", user=s["user"], ip=self._ip())
            return self._json({"ok": True}, extra={"Set-Cookie": "lr_session=; Max-Age=0; Path=/"})
        bad, s = self._gate(path)
        if bad is False:
            return
        if path == "/api/unmask":
            run, reason = str(body.get("run", "")), str(body.get("reason", "")).strip()[:300]
            if not valid_run(run, allow_oplog=True):
                return self._json({"error": "bad run"}, 400)
            if not privacy.can_unmask(s["role"]):
                oplog("denied", user=s["user"], path="/api/unmask", run_id=run, ip=self._ip())
                return self._json({"error": "your role cannot unmask personal data"}, 403)
            if run != "oplog" and self._inside(OUT / "runs" / run / "result.json", OUT / "runs") is None:
                return self._json({"error": "unknown run"}, 400)
            if len(reason) < 4:
                return self._json({"error": "give a reason (it goes to the operation log)"}, 400)
            now = time.time()
            for k in [k for k, exp in list(UNMASK.items()) if exp <= now]:    # drop expired grants
                UNMASK.pop(k, None)
            UNMASK[(s["user"], s.get("token", "local"), run)] = now + UNMASK_S
            oplog("unmask", user=s["user"], role=s["role"], run_id=run, reason=reason, seconds=UNMASK_S, ip=self._ip())
            return self._json({"ok": True, "run": run, "expires_s": UNMASK_S})
        if path == "/api/review":
            run, doc = str(body.get("run", "")), str(body.get("doc_id", ""))[:200]
            decision, note = str(body.get("decision", "")), str(body.get("note", "")).strip()[:300]
            if not valid_run(run):
                return self._json({"error": "bad run"}, 400)
            rp = self._inside(OUT / "runs" / run / "result.json", OUT / "runs")
            if rp is None:
                return self._json({"error": "unknown run"}, 400)
            if decision not in export.DECISIONS + ("undo",):
                return self._json({"error": "decision must be one of " + ", ".join(export.DECISIONS + ("undo",))}, 400)
            result = json.loads(rp.read_text(encoding="utf-8"))
            known = ({d.get("doc_id") for d in result.get("docs", [])}
                     | {e.get("doc_id") for e in (result.get("period_check") or {}).get("exceptions", [])})
            if doc not in known:
                return self._json({"error": "unknown document"}, 400)
            review_log(run).add("review", doc_id=doc, decision=decision, note=note, user=s["user"], role=s["role"])
            oplog("review", user=s["user"], run_id=run, doc_id=doc, decision=decision, ip=self._ip())
            return self._json({"ok": True, "doc_id": doc, "decision": decision, "user": s["user"]})
        if path == "/api/correct":
            return self._correct(body, s)
        if path == "/api/agent-override":
            return self._agent_override(body, s)
        if path == "/api/run":
            st = str(body.get("set", "main"))[:64]
            resume = str(body.get("resume") or "")
            live = bool(body.get("live")) or bool(resume)
            if st not in {i["set"] for i in inboxes()}:
                return self._json({"error": "unknown inbox"}, 400)
            if live and s["role"] != "admin":
                oplog("denied", user=s["user"], path="/api/run live", ip=self._ip())
                return self._json({"error": "only an admin can run the model live"}, 403)
            reuse = None
            if resume:
                old = next((r for r in interrupted_runs(st) if r["run_id"] == resume), None) if valid_run(resume) else None
                try:
                    reuse = set(json.loads((OUT / "runs" / resume / "job.json").read_text(encoding="utf-8"))["done"]) \
                        if old else None
                except (OSError, ValueError, KeyError, TypeError):
                    reuse = None
                if reuse is None:
                    return self._json({"error": "nothing to resume for that run"}, 400)
            with RUN_LOCK:      # one check at a time per inbox: a second press (or a second tab) joins the first
                cur = JOBS.get(ACTIVE.get(st) or "")
                if cur and cur.get("status") in RUNNING:
                    return self._json({"error": "this inbox is already being checked", "job": public_job(cur)}, 409)
                jid = uuid.uuid4().hex[:10]
                run_id = f"{st}-{time.strftime('%Y%m%d-%H%M%S')}-{jid[:4]}-{'live' if live else 'replay'}"
                JOBS[jid] = {"id": jid, "set": st, "live": live, "status": "queued", "i": 0, "n": 0, "run_id": run_id,
                             "user": s["user"], "started": time.time(), "_done": [],
                             **({"resumed_from": resume, "n_reused": len(reuse)} if resume else {})}
                ACTIVE[st] = jid
            if resume:      # the old folder says where its documents went
                op = OUT / "runs" / resume / "job.json"
                try:
                    oj = json.loads(op.read_text(encoding="utf-8"))
                    op.write_text(json.dumps(oj | {"status": "resumed", "resumed_by": run_id}, ensure_ascii=False),
                                  encoding="utf-8")
                except (OSError, ValueError):
                    pass
            save_job(JOBS[jid])
            oplog("run_start", user=s["user"], inbox=st, live=live, ip=self._ip(), run_id=run_id,
                  **({"resumed_from": resume} if resume else {}))
            threading.Thread(target=run_job, args=(jid, st, live, s["user"], reuse, resume or None), daemon=True).start()
            return self._json(public_job(JOBS[jid]))
        return self._json({"error": "not found"}, 404)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that does the TLS handshake in the request thread (a slow client cannot stall accept)."""
    ssl_ctx: ssl.SSLContext | None = None

    def finish_request(self, request, client_address):
        if self.ssl_ctx is None:
            return super().finish_request(request, client_address)
        request.settimeout(H.timeout)
        try:
            tls = self.ssl_ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):     # plain HTTP, port scans, aborted handshakes: nothing to answer
            return
        with tls:
            super().finish_request(tls, client_address)


def main():
    global OPLOG
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--cert", help="TLS certificate (PEM); serve HTTPS")
    ap.add_argument("--key", help="TLS private key (PEM) for --cert")
    ap.add_argument("--proxy-origin", metavar="URL",
                    help="local mode only: also accept requests forwarded by an authenticating reverse proxy on this "
                         "machine whose browser origin is URL (e.g. JupyterLab's /proxy/8780/ on a remote host)")
    ap.add_argument("--proxy-host", metavar="HOST:PORT",
                    help="with --proxy-origin: the Host header that proxy sends, when it is not the origin's host")
    ap.add_argument("--not-strict", action="store_true",
                    help="accept a value on one source (development only); the app checks strictly by default")
    ap.add_argument("--all-inboxes", action="store_true",
                    help="list every inbox under data/ (development); by default only those in web/inboxes.json")
    ap.add_argument("--behind-tls-proxy", action="store_true",
                    help="plain HTTP here, but only an HTTPS reverse proxy reaches this port; marks the cookie Secure")
    a = ap.parse_args()
    # The app always runs strict reading unless told not to (docs/strict_draft.md): a filing value needs two agreeing
    # sources, otherwise a person sees it. LEDGER_STRICT_READ=0 or --not-strict turns it off for development.
    pipeline.STRICT = not a.not_strict and os.environ.get("LEDGER_STRICT_READ", "1") != "0"
    if bool(a.cert) != bool(a.key):
        sys.exit("--cert and --key go together.")
    CFG["all_inboxes"] = a.all_inboxes
    CFG["users"] = userdb.load()
    CFG["local"] = not CFG["users"]
    CFG["host"] = a.host
    if a.proxy_origin:
        u = urlparse(a.proxy_origin)
        if u.scheme != "https" or not u.netloc or u.path not in ("", "/") or not is_loopback(a.host):
            sys.exit("--proxy-origin needs an https://host origin, and the server must listen on 127.0.0.1")
        CFG["proxy_origin"] = f"https://{u.netloc}"
        CFG["proxy_hosts"] = {u.netloc.lower()} | ({a.proxy_host.strip().lower()} if a.proxy_host else set())
    elif a.proxy_host:
        sys.exit("--proxy-host goes with --proxy-origin")
    CFG["tls"] = bool(a.cert)
    CFG["secure"] = bool(a.cert) or a.behind_tls_proxy
    if CFG["local"] and not is_loopback(a.host):
        sys.exit(f"No user accounts in {userdb.users_path()}. Add one first (python -m web.users add NAME --role admin); "
                 "without accounts Ledger Room only listens on 127.0.0.1.")
    if not is_loopback(a.host) and not CFG["tls"] and not a.behind_tls_proxy:
        sys.exit(f"Refusing to serve {a.host} over plain HTTP: passwords and session cookies would cross the LAN "
                 "unencrypted. Start with --cert CERT.pem --key KEY.pem, or with --behind-tls-proxy when an HTTPS "
                 "reverse proxy is the only way to reach this port.")
    ctx = None
    if CFG["tls"]:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(a.cert, a.key)
    OPLOG = AuditLog(OUT / "oplog.jsonl", append=True)
    oplog("server_start", host=a.host, port=a.port, mode="single local user" if CFG["local"] else
          f"{len(CFG['users'])} accounts", key_file=str(keys.audit_key_path()), strict=pipeline.STRICT)
    enc = privacy.encryption_self_check(DATA)       # warns on stderr when not encrypted; never blocks
    oplog("encryption_check", status=enc["status"], volume=enc.get("volume"), method=enc.get("method"))
    srv = Server((a.host, a.port), H)
    srv.ssl_ctx = ctx
    CFG["port"] = srv.server_address[1]
    mode = ("single local user, no login (no accounts yet)" if CFG["local"]
            else f"login required, {len(CFG['users'])} account(s)")
    scheme = "https" if CFG["tls"] else "http"
    print(f"Ledger Room on {scheme}://{a.host}:{CFG['port']}  [{mode}]  (synthetic data; no outbound network calls)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
