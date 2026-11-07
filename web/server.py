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

from ledger_room import export, keys, pipeline, privacy  # noqa: E402
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
PUBLIC = {"/", "/index.html", "/api/login", "/api/me", "/api/logout"}
ADMIN_ONLY = {"/api/audit-log", "/api/oplog"}
UNMASK: dict[tuple, float] = {}      # (user, session token, run) -> expiry of an unmask grant
UNMASK_S = 600
IMG_EXT = {".jpg", ".jpeg", ".png"}


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
                             platform="auto", run_id=f"{set_name}-{time.strftime('%Y%m%d-%H%M%S')}-{'live' if live else 'replay'}")
        job.update(status="done", run_id=res["summary"]["run_id"], summary=res["summary"])
        oplog("run_done", user=user, run_id=res["summary"]["run_id"], n_exceptions=res["summary"]["n_exceptions"])
    except Exception as e:  # surface the error in the UI
        job.update(status="error", error=f"{e}", trace=traceback.format_exc()[-2000:])


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
        return (self.headers.get("Host") or "").strip().lower() in {f"127.0.0.1:{port}", f"localhost:{port}",
                                                                    f"[::1]:{port}"}

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None:
            scheme = "https" if CFG["tls"] or CFG["secure"] else "http"
            if origin != f"{scheme}://{self.headers.get('Host') or ''}":
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
        if path in ("/api/result", "/api/export", "/api/audit-log") and not valid_run(q.get("run", "")):
            return self._json({"error": "bad run"}, 400)
        if path in ("/", "/index.html"):
            return self._file(STATIC / "index.html", STATIC)
        if path.startswith("/static/"):
            return self._file(STATIC / path[len("/static/"):], STATIC)
        if path == "/api/me":
            me = self._session()
            return self._json({"user": me["user"], "role": me["role"], "local": CFG["local"],
                               "can_unmask": privacy.can_unmask(me["role"])} if me else
                              {"user": None, "local": CFG["local"]})
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
            return self._json({"set": st, "clients": [privacy.mask_client(c) for c in clients.values()],
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
            if clear:
                return self._file(p, OUT / "runs")
            return self._json(privacy.mask_result(json.loads(p.read_text(encoding="utf-8"))))
        if path == "/api/export":
            run = q.get("run", "")
            clear = q.get("unmask") == "1" and self._unmasked(s, run)
            oplog("export_media_csv", user=s["user"], run_id=run, ip=self._ip(), masked=not clear)
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
            return self._json(j or {"error": "no such job"}, 200 if j else 404)
        if path == "/api/status":
            return self._json({"model_loaded": _reader is not None,
                               "model": _reader.model_id if _reader else None,
                               "device": _reader.device if _reader else None})
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
        if path == "/api/run":
            st = str(body.get("set", "main"))[:64]
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
    ap.add_argument("--behind-tls-proxy", action="store_true",
                    help="plain HTTP here, but only an HTTPS reverse proxy reaches this port; marks the cookie Secure")
    a = ap.parse_args()
    if bool(a.cert) != bool(a.key):
        sys.exit("--cert and --key go together.")
    CFG["users"] = userdb.load()
    CFG["local"] = not CFG["users"]
    CFG["host"] = a.host
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
          f"{len(CFG['users'])} accounts", key_file=str(keys.audit_key_path()))
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
