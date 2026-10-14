"""Ledger Room local web app. Python standard library only; no external assets; works offline.

python -m web.server                 # http://127.0.0.1:8780, this machine only
python -m web.server --host 0.0.0.0  # office LAN: refuses to start until user accounts exist (python -m web.users add)

Access control (bookkeepers' safeguard regulation Arts. 15, 17 and 19):
  * binds to 127.0.0.1 unless told otherwise;
  * with accounts in users.json every request except the login page needs a session; roles admin / staff;
  * without accounts it runs as one local user and only on the loopback address;
  * an operation log (who logged in, ran, viewed, exported) is appended with the same HMAC chain as the audit log.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import mimetypes
import secrets
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # allow `python web/server.py`

from ledger_room import keys, pipeline  # noqa: E402
from ledger_room.pipeline import DATA, OUT, AuditLog  # noqa: E402
from web import users as userdb  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web" / "static"
JOBS: dict[str, dict] = {}
SESSIONS: dict[str, dict] = {}
FAILS: dict[str, list] = {}           # name -> [count, locked_until]
SESSION_S = 8 * 3600
MAX_FAILS, LOCK_S = 5, 300
CFG = {"users": {}, "local": True, "host": "127.0.0.1"}
OPLOG: AuditLog | None = None
_reader = None
_reader_lock = threading.Lock()
PUBLIC = {"/", "/index.html", "/api/login", "/api/me", "/api/logout"}
ADMIN_ONLY = {"/api/audit-log", "/api/oplog"}


def get_reader():
    global _reader
    with _reader_lock:
        if _reader is None:
            from ledger_room.reader import Reader
            _reader = Reader()
    return _reader


def oplog(event: str, **kw) -> None:
    if OPLOG is not None:
        OPLOG.add(event, **kw)


def inboxes() -> list[dict]:
    out = []
    for p in sorted(DATA.iterdir()):
        cj = p / "clients.json"
        if p.name == "out" or not cj.exists():
            continue
        cfg = json.loads(cj.read_text(encoding="utf-8"))
        n = len(list((p / "clients").glob("*/*/*.jpg")))
        out.append({"set": p.name, "n_docs": n, "n_clients": len(cfg["clients"]), "layout_family": cfg.get("layout_family")})
    return out


def run_job(job_id: str, set_name: str, live: bool, user: str):
    job = JOBS[job_id]
    try:
        reader = None
        if live:
            job["status"] = "loading model"
            reader = get_reader()
            job["model"], job["device"] = reader.model_id, reader.device
        job["status"] = "reading"

        def prog(i, n, doc_id, res):
            job.update(i=i, n=n, doc_id=doc_id, last_s=res["timing"].get("total_s"), cached=res.get("cached", False))
            if not live:
                time.sleep(0.12)   # replay pacing so the progress is visible; reads come from cache

        from ledger_room.registry import load_default
        res = pipeline.audit(set_name, reader, cache=not live, progress=prog, registry=load_default(), operator=user,
                             run_id=f"{set_name}-{time.strftime('%Y%m%d-%H%M%S')}-{'live' if live else 'replay'}")
        job.update(status="done", run_id=res["summary"]["run_id"], summary=res["summary"])
        oplog("run_done", user=user, run_id=res["summary"]["run_id"], n_exceptions=res["summary"]["n_exceptions"])
    except Exception as e:  # surface the error in the UI
        job.update(status="error", error=f"{e}", trace=traceback.format_exc()[-2000:])


class H(BaseHTTPRequestHandler):
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
            self.send_header(k, v)
        self.end_headers()

    def _json(self, obj, code=200, extra: dict | None = None):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._headers("application/json; charset=utf-8", len(b), code, extra)
        self.wfile.write(b)

    def _file(self, p: Path, base: Path, download: str | None = None):
        try:
            p = p.resolve()
            p.relative_to(base.resolve())
        except Exception:
            return self._json({"error": "not found"}, 404)
        if not p.is_file():
            return self._json({"error": "not found"}, 404)
        b = p.read_bytes()
        extra = {"Content-Disposition": f'attachment; filename="{download}"'} if download else None
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

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10_000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = unquote(u.path)
        bad, s = self._gate(path)
        if bad is False:
            return
        if path in ("/", "/index.html"):
            return self._file(STATIC / "index.html", STATIC)
        if path.startswith("/static/"):
            return self._file(STATIC / path[len("/static/"):], STATIC)
        if path == "/api/me":
            me = self._session()
            return self._json({"user": me["user"], "role": me["role"], "local": CFG["local"]} if me else
                              {"user": None, "local": CFG["local"]})
        if path.startswith("/img/"):
            return self._file(DATA / path[len("/img/"):], DATA)
        if path.startswith("/out/"):
            return self._file(OUT / path[len("/out/"):], OUT)
        if path == "/api/inboxes":
            return self._json(inboxes())
        if path == "/api/inbox":
            st = q.get("set", "main")
            if st not in {i["set"] for i in inboxes()}:
                return self._json({"error": "unknown inbox"}, 400)
            clients, docs = pipeline.load_inbox(st)
            return self._json({"set": st, "clients": list(clients.values()),
                               "docs": [{k: v for k, v in d.items() if k != "path"} | {"image": f"{st}/clients/{d['doc_id']}"}
                                        for d in docs]})
        if path == "/api/runs":
            rd = OUT / "runs"
            runs = sorted([p.name for p in rd.iterdir() if (p / "result.json").exists()], reverse=True) if rd.exists() else []
            return self._json(runs)
        if path == "/api/result":
            oplog("view_result", user=s["user"], run_id=q.get("run", ""), ip=self._ip())
            return self._file(OUT / "runs" / q.get("run", "") / "result.json", OUT / "runs")
        if path == "/api/export":
            oplog("export_media_csv", user=s["user"], run_id=q.get("run", ""), ip=self._ip())
            return self._file(OUT / "runs" / q.get("run", "") / "export_media.csv", OUT / "runs",
                              download=f"ledger_room_{q.get('run', 'run')}_media.csv")
        if path in ("/api/audit-log", "/api/oplog"):
            if path == "/api/oplog":
                p = OUT / "oplog.jsonl"
            else:
                p = OUT / "runs" / q.get("run", "") / "audit_log.jsonl"
                try:
                    p.resolve().relative_to((OUT / "runs").resolve())
                except Exception:
                    return self._json({"error": "bad run"}, 400)
            if not p.exists():
                return self._json({"error": "not found"}, 404)
            oplog("view_" + path.rsplit("/", 1)[-1].replace("-", "_"), user=s["user"], run_id=q.get("run"), ip=self._ip())
            v = AuditLog.verify_detail(p, keys.audit_key(create=False))
            entries = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
            return self._json({"verified": v["chain_ok"] and v["mac_ok"] is not False, "chain_ok": v["chain_ok"],
                               "mac_ok": v["mac_ok"], "entries_verified": v["n"], "entries": entries[-500:]})
        if path.startswith("/api/job/"):
            j = JOBS.get(path.rsplit("/", 1)[-1])
            return self._json(j or {"error": "no such job"}, 200 if j else 404)
        if path == "/api/status":
            return self._json({"model_loaded": _reader is not None,
                               "model": _reader.model_id if _reader else None,
                               "device": _reader.device if _reader else None})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/api/login":
            b = self._body()
            name, pw = str(b.get("user", ""))[:64], str(b.get("password", ""))[:256]
            if CFG["local"]:
                return self._json({"user": "local", "role": "admin", "local": True})
            f = FAILS.get(name, [0, 0])
            if f[1] > time.time():
                oplog("login_locked", user=name, ip=self._ip())
                return self._json({"error": "too many attempts; try again in a few minutes"}, 429)
            role = userdb.check(CFG["users"], name, pw)
            if role is None:
                f[0] += 1
                if f[0] >= MAX_FAILS:
                    f = [0, time.time() + LOCK_S]
                FAILS[name] = f
                oplog("login_failed", user=name, ip=self._ip())
                return self._json({"error": "wrong user name or password"}, 401)
            FAILS.pop(name, None)
            tok = secrets.token_urlsafe(32)
            SESSIONS[tok] = {"user": name, "role": role, "exp": time.time() + SESSION_S}
            oplog("login", user=name, role=role, ip=self._ip())
            return self._json({"user": name, "role": role, "local": False},
                              extra={"Set-Cookie": f"lr_session={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_S}"})
        if path == "/api/logout":
            s = self._session()
            if s and s.get("token"):
                SESSIONS.pop(s["token"], None)
                oplog("logout", user=s["user"], ip=self._ip())
            return self._json({"ok": True}, extra={"Set-Cookie": "lr_session=; Max-Age=0; Path=/"})
        bad, s = self._gate(path)
        if bad is False:
            return
        if path == "/api/run":
            body = self._body()
            st = body.get("set", "main")
            live = bool(body.get("live"))
            if st not in {i["set"] for i in inboxes()}:
                return self._json({"error": "unknown inbox"}, 400)
            if live and s["role"] != "admin":
                oplog("denied", user=s["user"], path="/api/run live", ip=self._ip())
                return self._json({"error": "only an admin can run the model live"}, 403)
            jid = uuid.uuid4().hex[:10]
            JOBS[jid] = {"id": jid, "set": st, "live": live, "status": "queued", "i": 0, "n": 0}
            oplog("run_start", user=s["user"], inbox=st, live=live, ip=self._ip())
            threading.Thread(target=run_job, args=(jid, st, live, s["user"]), daemon=True).start()
            return self._json(JOBS[jid])
        return self._json({"error": "not found"}, 404)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main():
    global OPLOG
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    a = ap.parse_args()
    CFG["users"] = userdb.load()
    CFG["local"] = not CFG["users"]
    CFG["host"] = a.host
    if CFG["local"] and not is_loopback(a.host):
        sys.exit(f"No user accounts in {userdb.users_path()}. Add one first (python -m web.users add NAME --role admin); "
                 "without accounts Ledger Room only listens on 127.0.0.1.")
    OPLOG = AuditLog(OUT / "oplog.jsonl", append=True)
    oplog("server_start", host=a.host, port=a.port, mode="single local user" if CFG["local"] else
          f"{len(CFG['users'])} accounts", key_file=str(keys.audit_key_path()))
    srv = ThreadingHTTPServer((a.host, a.port), H)
    mode = ("single local user, no login (no accounts yet)" if CFG["local"]
            else f"login required, {len(CFG['users'])} account(s)")
    print(f"Ledger Room on http://{a.host}:{a.port}  [{mode}]  (synthetic data; no outbound network calls)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
