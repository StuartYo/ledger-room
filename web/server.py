"""Ledger Room local web app. Python standard library only; no external assets; works offline.

python -m web.server                 # http://127.0.0.1:8780
python -m web.server --host 0.0.0.0  # share on the office LAN (the documents still never leave the machine)
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # allow `python web/server.py`

from ledger_room import pipeline  # noqa: E402
from ledger_room.pipeline import DATA, OUT, AuditLog

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web" / "static"
JOBS: dict[str, dict] = {}
_reader = None
_reader_lock = threading.Lock()


def get_reader():
    global _reader
    with _reader_lock:
        if _reader is None:
            from ledger_room.reader import Reader
            _reader = Reader()
    return _reader


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


def run_job(job_id: str, set_name: str, live: bool):
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

        res = pipeline.audit(set_name, reader, cache=not live, progress=prog,
                             run_id=f"{set_name}-{time.strftime('%Y%m%d-%H%M%S')}-{'live' if live else 'replay'}")
        job.update(status="done", run_id=res["summary"]["run_id"], summary=res["summary"])
    except Exception as e:  # surface the error in the UI
        job.update(status="error", error=f"{e}", trace=traceback.format_exc()[-2000:])


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _file(self, p: Path, base: Path):
        try:
            p = p.resolve()
            p.relative_to(base.resolve())
        except Exception:
            return self._json({"error": "not found"}, 404)
        if not p.is_file():
            return self._json({"error": "not found"}, 404)
        b = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = unquote(u.path)
        if path in ("/", "/index.html"):
            return self._file(STATIC / "index.html", STATIC)
        if path.startswith("/static/"):
            return self._file(STATIC / path[len("/static/"):], STATIC)
        if path.startswith("/img/"):
            return self._file(DATA / path[len("/img/"):], DATA)
        if path.startswith("/out/"):
            return self._file(OUT / path[len("/out/"):], OUT)
        if path == "/api/inboxes":
            return self._json(inboxes())
        if path == "/api/inbox":
            s = q.get("set", "main")
            clients, docs = pipeline.load_inbox(s)
            return self._json({"set": s, "clients": list(clients.values()),
                               "docs": [{k: v for k, v in d.items() if k != "path"} | {"image": f"{s}/clients/{d['doc_id']}"}
                                        for d in docs]})
        if path == "/api/runs":
            rd = OUT / "runs"
            runs = sorted([p.name for p in rd.iterdir() if (p / "result.json").exists()], reverse=True) if rd.exists() else []
            return self._json(runs)
        if path == "/api/result":
            p = OUT / "runs" / q.get("run", "") / "result.json"
            return self._file(p, OUT / "runs")
        if path == "/api/audit-log":
            p = OUT / "runs" / q.get("run", "") / "audit_log.jsonl"
            try:
                p.resolve().relative_to((OUT / "runs").resolve())
            except Exception:
                return self._json({"error": "bad run"}, 400)
            if not p.exists():
                return self._json({"error": "not found"}, 404)
            ok, n = AuditLog.verify(p)
            entries = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
            return self._json({"verified": ok, "entries_verified": n, "entries": entries})
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
        if u.path == "/api/run":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            s = body.get("set", "main")
            if s not in {i["set"] for i in inboxes()}:
                return self._json({"error": "unknown inbox"}, 400)
            jid = uuid.uuid4().hex[:10]
            JOBS[jid] = {"id": jid, "set": s, "live": bool(body.get("live")), "status": "queued", "i": 0, "n": 0}
            threading.Thread(target=run_job, args=(jid, s, bool(body.get("live"))), daemon=True).start()
            return self._json(JOBS[jid])
        return self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"Ledger Room on http://{a.host}:{a.port}  (synthetic data; no outbound network calls)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
