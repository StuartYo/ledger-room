"""Web server hardening: paths, run ids, request bodies, CSRF, DNS rebinding, login lockout, audit-log lines and tail."""
import http.client
import json
import pathlib
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from ledger_room.pipeline import AuditLog
from web import server, users

KEY = b"s" * 32


def _is_unc(p) -> bool:
    s = str(p)
    return s.startswith("\\\\") or s.startswith("//")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_SECRETS_DIR", str(tmp_path / "secrets"))
    data, out = tmp_path / "data", tmp_path / "data" / "out"
    run = out / "runs" / "r1"
    (run / "crops").mkdir(parents=True)
    (run / "crops" / "x.png").write_bytes(b"png")
    (run / "result.json").write_text(json.dumps({"summary": {}}), encoding="utf-8")
    (run / "export_media.csv").write_text("client_id\r\nC01\r\n", encoding="utf-8")
    monkeypatch.setattr(server, "DATA", data)
    monkeypatch.setattr(server, "OUT", out)
    monkeypatch.setattr(server.keys, "audit_key", lambda create=True: KEY)
    monkeypatch.setattr(server, "OPLOG", AuditLog(out / "oplog.jsonl", key=KEY, append=True))
    monkeypatch.setattr(server, "UNMASK", {})
    monkeypatch.setattr(server, "FAILS", {})
    monkeypatch.setattr(server, "IP_FAILS", {})
    monkeypatch.setitem(server.CFG, "users", {})
    monkeypatch.setitem(server.CFG, "local", True)
    monkeypatch.setitem(server.CFG, "tls", False)
    monkeypatch.setitem(server.CFG, "secure", False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
    port = srv.server_address[1]
    monkeypatch.setitem(server.CFG, "port", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port, out
    srv.shutdown()
    srv.server_close()


def req(port, method, path, headers=None, body=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    h = {"Host": f"127.0.0.1:{port}" if host is None else host, **(headers or {})}
    if body is not None and "Content-Length" not in h:
        h["Content-Length"] = str(len(body))
    for k, v in h.items():
        c.putheader(k, v)
    c.endheaders(body)
    r = c.getresponse()
    b = r.read()
    c.close()
    return r.status, b, r


def post(port, path, obj, **kw):
    return req(port, "POST", path, {"Content-Type": "application/json", **kw.pop("headers", {})},
               json.dumps(obj).encode(), **kw)


def test_unc_backslash_and_dotdot_paths_never_reach_the_filesystem(app, monkeypatch):
    port, _ = app
    touched = []
    for meth in ("resolve", "is_file", "exists", "stat", "read_bytes"):
        orig = getattr(pathlib.Path, meth)

        def guard(self, *a, _orig=orig, _m=meth, **k):
            if _is_unc(self):
                touched.append((_m, str(self)))
                raise AssertionError(f"filesystem access to {self}")
            return _orig(self, *a, **k)
        monkeypatch.setattr(pathlib.Path, meth, guard)
    assert server.H._inside(server.STATIC / "\\\\host\\share\\x", server.STATIC) is None
    for p in ("/static/%5C%5Chost%5Cshare%5Cx", "/static//host/share/x", "/static/%2F%2Fhost%2Fshare%2Fx",
              "/static/..%2F..%2Fserver.py", "/static/a%5C..%5C..%5Cserver.py", "/static/C:%5CWindows%5Cwin.ini",
              "/static/./app.js", "/static/", "/img/%5C%5Chost%5Cshare%5Cx.jpg", "/img/main//x.jpg",
              "/out/%5C%5Chost%5Cshare%5Ccrops%5Cx.png", "/out/runs/r1/crops/../crops/x.png",
              "/out/runs/r1/crops/x.png%00.png"):
        assert req(port, "GET", p)[0] == 404, p
    assert not touched
    assert req(port, "GET", "/static/app.js")[0] == 200
    assert req(port, "GET", "/out/runs/r1/crops/x.png")[0] == 200


def test_bad_run_ids_are_400(app):
    port, _ = app
    for p in ("/api/result?run=..%2F..%2Fx", "/api/result?run=", "/api/result?run=..",
              "/api/export?run=a%0D%0ASet-Cookie:%20x=1", "/api/export?run=a%22b", "/api/export?run=" + "a" * 129,
              "/api/audit-log?run=..", "/api/audit-log?run=%5C%5Chost%5Cshare", "/api/audit-log?run=oplog%2F.."):
        assert req(port, "GET", p)[0] == 400, p
    for r in ("../x", "a/b", "", "x\r\ny", ".."):
        assert post(port, "/api/unmask", {"run": r, "reason": "check it"})[0] == 400, r
    assert req(port, "GET", "/api/result?run=r1")[0] == 200
    assert post(port, "/api/unmask", {"run": "oplog", "reason": "check it"})[0] == 200


def test_header_values_cannot_inject():
    assert server.attachment('a"b\r\nSet-Cookie: x') == 'attachment; filename="abSet-Cookie: x"'


def test_export_masked_needs_the_result(app):
    port, out = app
    (out / "runs" / "r1" / "result.json").unlink()
    assert req(port, "GET", "/api/export?run=r1")[0] == 404


def test_bad_content_length(app):
    port, _ = app
    h = {"Content-Type": "application/json"}
    assert req(port, "POST", "/api/run", {**h, "Content-Length": "-1"})[0] == 400
    assert req(port, "POST", "/api/run", {**h, "Content-Length": "abc"})[0] == 400
    assert req(port, "POST", "/api/run", {**h, "Content-Length": "999999"})[0] == 413
    assert req(port, "POST", "/api/run", h, b"[1, 2]")[0] == 400


def test_post_needs_json_and_same_origin(app):
    port, _ = app
    body = b'{"user": "a", "password": "b"}'
    for path in ("/api/login", "/api/logout", "/api/run", "/api/unmask"):
        assert req(port, "POST", path, {"Content-Type": "text/plain"}, body)[0] == 415, path
        assert req(port, "POST", path, {"Content-Type": "application/x-www-form-urlencoded"}, b"a=b")[0] == 415, path
        assert req(port, "POST", path, None, body)[0] == 415, path
        assert post(port, path, {}, headers={"Origin": "http://evil.example"})[0] == 403, path
        assert post(port, path, {}, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403, path
    ok = {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"}
    assert post(port, "/api/logout", {}, headers=ok)[0] == 200


def test_local_mode_checks_host_header(app):
    port, _ = app
    for host in ("evil.example", f"evil.example:{port}", f"127.0.0.1:{port + 1}", "127.0.0.1", ""):
        assert req(port, "GET", "/api/me", host=host)[0] == 403, host
        assert post(port, "/api/logout", {}, host=host)[0] == 403, host
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
        assert req(port, "GET", "/api/me", host=host)[0] == 200, host


def test_login_lockout_holds_under_parallel_guesses(app, monkeypatch):
    port, _ = app
    monkeypatch.setitem(server.CFG, "local", False)
    monkeypatch.setitem(server.CFG, "users", {"amy": {"role": "admin"}})
    calls = []
    lock = threading.Lock()

    def slow_check(db, name, pw):
        with lock:
            calls.append(name)
        time.sleep(0.2)
        return None
    monkeypatch.setattr(server.userdb, "check", slow_check)
    codes = []

    def guess(i):
        codes.append(post(port, "/api/login", {"user": "amy", "password": f"guess {i}"})[0])
    ts = [threading.Thread(target=guess, args=(i,)) for i in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) <= server.MAX_FAILS
    assert codes.count(401) == len(calls) and codes.count(429) == 20 - len(calls)
    assert post(port, "/api/login", {"user": "amy", "password": "x"})[0] == 429


def test_login_per_ip_limit(app, monkeypatch):
    port, _ = app
    monkeypatch.setitem(server.CFG, "local", False)
    monkeypatch.setitem(server.CFG, "users", {"amy": users.make_user("right password 1", "admin")})
    monkeypatch.setattr(server.userdb, "check", lambda db, name, pw: None)
    codes = [post(port, "/api/login", {"user": f"name{i}", "password": "x"})[0] for i in range(server.MAX_IP_FAILS + 3)]
    assert codes[:server.MAX_IP_FAILS] == [401] * server.MAX_IP_FAILS
    assert codes[server.MAX_IP_FAILS:] == [429] * 3


def test_line_separator_in_a_logged_field_keeps_one_line(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_SECRETS_DIR", str(tmp_path / "secrets"))
    p = tmp_path / "op.jsonl"
    log = AuditLog(p, key=KEY)
    log.add("login_failed", user="amy x\x85y z")
    log.add("login", user="ben")
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") == 2 and " " not in text and "\x85" not in text
    assert json.loads(text.split("\n")[0])["user"] == "amy x\x85y z"
    assert AuditLog.verify_detail(p, KEY, head=True) == {"chain_ok": True, "mac_ok": True, "n": 2, "tail_ok": True}
    AuditLog(p, key=KEY, append=True).add("logout", user="ben")       # resumes after the odd line
    es = [json.loads(x) for x in AuditLog.lines(p)]
    assert [e["event"] for e in es] == ["login_failed", "login", "logout"]
    assert AuditLog.verify(p, KEY) == (True, 3)


def test_old_style_line_with_raw_separator_still_verifies(tmp_path, monkeypatch):
    """Logs written before the fix (raw non-ASCII on disk) keep verifying with the "\\n"-only reader."""
    monkeypatch.setenv("LEDGER_SECRETS_DIR", str(tmp_path / "secrets"))
    p = tmp_path / "old.jsonl"
    AuditLog(p, key=KEY).add("login_failed", user="amy x 張")
    e = json.loads(p.read_text(encoding="utf-8").split("\n")[0])
    p.write_text(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    assert " " in p.read_text(encoding="utf-8")
    assert AuditLog.verify(p, KEY) == (True, 1)


def test_bad_last_line_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_SECRETS_DIR", str(tmp_path / "secrets"))
    p = tmp_path / "op.jsonl"
    AuditLog(p, key=KEY).add("login", user="a")
    with p.open("a", encoding="utf-8") as f:
        f.write('{"seq": 2, "half a line\n')
    assert AuditLog.verify_detail(p, KEY)["chain_ok"] is False       # no exception
    AuditLog(p, key=KEY, append=True).add("server_start")
    es = [json.loads(x) for x in AuditLog.lines(p) if x.startswith('{"') and "half" not in x]
    assert [e["event"] for e in es] == ["login", "log_tail_broken", "server_start"]
    assert es[1]["unreadable_lines"] == 1


def test_truncated_tail_is_detected(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_SECRETS_DIR", str(tmp_path / "secrets"))
    p = tmp_path / "audit_log.jsonl"
    log = AuditLog(p, key=KEY)
    for i in range(3):
        log.add("doc_read", doc_id=f"d{i}")
    assert AuditLog.verify_detail(p, KEY, head=True)["tail_ok"] is True
    assert "tail_ok" not in AuditLog.verify_detail(p, KEY)           # the old call keeps the old keys
    lines = AuditLog.lines(p)
    p.write_text("".join(x + "\n" for x in lines[:-1]), encoding="utf-8")
    d = AuditLog.verify_detail(p, KEY, head=True)
    assert d["chain_ok"] is True and d["mac_ok"] is True and d["n"] == 2 and d["tail_ok"] is False
    p.write_text("", encoding="utf-8")
    assert AuditLog.verify_detail(p, KEY, head=True)["tail_ok"] is False
    p.unlink()
    assert AuditLog.verify_detail(p, KEY, head=True)["tail_ok"] is False
    # a restart in append mode records the gap in the log itself
    p.write_text("".join(x + "\n" for x in lines[:-1]), encoding="utf-8")
    AuditLog(p, key=KEY, append=True)
    es = [json.loads(x) for x in AuditLog.lines(p)]
    assert es[-1]["event"] == "log_tail_broken" and es[-1]["head_seq"] == 3 and es[-1]["last_seq"] == 2


def test_server_verified_flag_includes_tail(app):
    port, out = app
    p = out / "runs" / "r1" / "audit_log.jsonl"
    log = AuditLog(p, key=KEY)
    for i in range(3):
        log.add("doc_read", doc_id=f"d{i}")
    b = json.loads(req(port, "GET", "/api/audit-log?run=r1")[1])
    assert b["verified"] is True and b["tail_ok"] is True
    p.write_text("".join(x + "\n" for x in AuditLog.lines(p)[:-1]), encoding="utf-8")
    b = json.loads(req(port, "GET", "/api/audit-log?run=r1")[1])
    assert b["verified"] is False and b["tail_ok"] is False and b["chain_ok"] is True


def test_proxy_origin_admits_only_that_host_and_origin(app, monkeypatch):
    port, _ = app
    proxy = "cloud-1.example.com"
    assert req(port, "GET", "/api/status", host=proxy)[0] == 403
    monkeypatch.setitem(server.CFG, "proxy_origin", f"https://{proxy}")
    assert req(port, "GET", "/api/status", host=proxy)[0] == 200
    assert req(port, "GET", "/api/status", host="evil.example.com")[0] == 403
    ok = {"Origin": f"https://{proxy}", "Sec-Fetch-Site": "same-origin"}
    bad = {"Origin": "https://evil.example.com", "Sec-Fetch-Site": "cross-site"}
    assert post(port, "/api/run", {"set": "nope"}, host=proxy, headers=bad)[0] == 403
    assert post(port, "/api/run", {"set": "nope"}, host=proxy, headers=ok)[0] != 403


def test_front_end_uses_relative_urls_so_it_works_under_a_proxy_path():
    root = pathlib.Path(server.__file__).parent / "static"
    for f in ("app.js", "index.html"):
        t = (root / f).read_text(encoding="utf-8")
        for bad in ('"/api/', "`/api/", '"/img/', "`/img/", '"/static/'):
            assert bad not in t, (f, bad)
