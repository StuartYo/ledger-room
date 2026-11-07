"""Display masking (safeguard regulation Art. 17, 顯示隱碼) and role-based unmask with an operation-log entry."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ledger_room import pipeline, privacy
from ledger_room.pipeline import AuditLog

KEY = b"k" * 32


@pytest.mark.parametrize("name,personal,masked", [
    ("王小明", True, "王○明"), ("王明", True, "王○"), ("歐陽小明", True, "歐○○明"), ("王子軒", True, "王○軒"),
    ("John Smith", True, "J*** S***"),
    ("青嵐室內設計有限公司", False, None), ("永固建材行", False, None), ("王記", False, None),
    ("全家便利商店", False, None), ("7-ELEVEN", False, None), ("Family Mart", False, None),
    ("買受人", False, None), ("未提供", False, None), (None, False, None), ("", False, None),
])
def test_personal_name_rule(name, personal, masked):
    assert privacy.is_personal_name(name) is personal
    if masked:
        assert privacy.mask_name(name) == masked


def test_free_text_masks_phone_id_email_and_labelled_names_but_keeps_ubn_and_invoice_no():
    s = ("負責人：王小明 手機 0912-345-678 電話 (02)2345-6789 身分證 A123456789 居留證 B800000001 "
         "amy.chen@example.com 統編 08807719 發票 AB12345678 日期 2026-09-14 金額 4652")
    m = privacy.mask_text(s)
    for leak in ("王小明", "345-678", "2345-6789", "A123456789", "B800000001", "amy.chen"):
        assert leak not in m
    assert "負責人：王○明" in m and "0912-***-678" in m and "A12****789" in m and "a***@example.com" in m
    for kept in ("08807719", "AB12345678", "2026-09-14", "4652"):
        assert kept in m
    assert privacy.mask_text("買受人：青嵐室內設計有限公司") == "買受人：青嵐室內設計有限公司"
    h = "a0912345678b" + "0" * 52                     # inside a hash: not a phone number
    assert privacy.mask_text(h) == h


def _result():
    return {
        "summary": {"run_id": "r1", "set": "main", "by_client": {"C01": 1}},
        "clients": [{"id": "C01", "name": "青嵐室內設計有限公司", "ubn": "91541552", "owner": "林美玲",
                     "phone": "0922333444"},
                    {"id": "C02", "name": "陳大文", "ubn": "12345675", "sole_proprietor": True}],
        "docs": [{"doc_id": "C01/2026-09/IMG_1.jpg", "client_id": "C01", "image": "main/clients/C01/2026-09/IMG_1.jpg",
                  "fields": {"seller_name": "張志豪", "seller_ubn": "08807719", "buyer_name": "青嵐室內設計有限公司",
                             "buyer_ubn": "91541552", "invoice_no": "AB12345678", "total": 4652,
                             "items": [{"desc": "代訂 張志豪 0912345678", "qty": 1, "amount": 4652}],
                             "note": "聯絡人：張志豪"}}],
        "exceptions": [{"doc_id": "C01/2026-09/IMG_1.jpg", "client_id": "C01", "rule_id": "BUYER_PRESUMED",
                        "message": "printed buyer name '張志豪' looks like this client",
                        "evidence": [{"field": "seller_name", "value": "張志豪", "crop": "runs/r1/crops/x.png"}],
                        "image": "main/clients/C01/2026-09/IMG_1.jpg"}],
        "rules": {"X": {"label": "王小明 rule text stays"}},
    }


def test_mask_result_masks_names_everywhere_and_keeps_ids():
    r = _result()
    m = privacy.mask_result(r)
    blob = json.dumps(m, ensure_ascii=False)
    for leak in ("張志豪", "林美玲", "0922333444", "0912345678", "陳大文"):
        assert leak not in blob, leak
    f = m["docs"][0]["fields"]
    assert f["seller_name"] == "張○豪" and f["buyer_name"] == "青嵐室內設計有限公司"
    assert f["seller_ubn"] == "08807719" and f["invoice_no"] == "AB12345678" and f["total"] == 4652
    assert m["docs"][0]["doc_id"] == "C01/2026-09/IMG_1.jpg" and m["exceptions"][0]["evidence"][0]["crop"] == "runs/r1/crops/x.png"
    assert m["clients"][0]["name"] == "青嵐室內設計有限公司" and m["clients"][1]["name"] == "陳○文"
    assert m["clients"][1]["ubn"] == "12345675" and m["masked"] is True
    assert r["docs"][0]["fields"]["seller_name"] == "張志豪"            # the original is not changed


def test_mask_csv_keeps_ubn_columns():
    text = "﻿client_id,client_ubn,doc_id,seller_ubn,invoice_no,note\r\nC01,91541552,C01/x.jpg,08807719,AB12345678,張志豪 0912345678\r\n"
    m = privacy.mask_csv(text, {"張志豪"})
    assert m.startswith("﻿") and "91541552" in m and "08807719" in m and "AB12345678" in m
    assert "張志豪" not in m and "0912345678" not in m


def test_only_unmask_roles_can_unmask():
    assert privacy.can_unmask("admin") and not privacy.can_unmask("staff") and not privacy.can_unmask(None)


# ---- through the web app: masked by default, unmask needs the role and a reason, and is logged ----

@pytest.fixture
def app(tmp_path, monkeypatch):
    from web import server, users
    data = tmp_path / "data"
    out = data / "out"
    run = out / "runs" / "main-r1"
    (run / "crops").mkdir(parents=True)
    (data / "main" / "clients" / "C01" / "2026-09").mkdir(parents=True)
    (data / "main" / "clients" / "C01" / "2026-09" / "IMG_1.jpg").write_bytes(b"\xff\xd8jpg")
    r = _result()
    (data / "main" / "clients.json").write_text(json.dumps({"clients": r["clients"]}, ensure_ascii=False), encoding="utf-8")
    (run / "result.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    (run / "export_media.csv").write_text("client_id,seller_ubn,note\r\nC01,08807719,張志豪 0912345678\r\n", encoding="utf-8")
    (run / "crops" / "x.png").write_bytes(b"png")
    AuditLog(run / "audit_log.jsonl", key=KEY).add("run_start", operator="amy")
    monkeypatch.setattr(server, "DATA", data)
    monkeypatch.setattr(server, "OUT", out)
    monkeypatch.setattr(pipeline, "DATA", data)
    monkeypatch.setattr(server.keys, "audit_key", lambda create=True: KEY)
    monkeypatch.setitem(server.CFG, "users", {"amy": users.make_user("admin password 1", "admin"),
                                              "ben": users.make_user("staff password 1", "staff")})
    monkeypatch.setitem(server.CFG, "local", False)
    monkeypatch.setattr(server, "OPLOG", AuditLog(out / "oplog.jsonl", key=KEY, append=True))
    monkeypatch.setattr(server, "UNMASK", {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", out
    srv.shutdown()


def _req(base, path, cookie=None, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def _login(base, user, pw):
    code, _, h = _req(base, "/api/login", body={"user": user, "password": pw})
    assert code == 200
    return h["Set-Cookie"].split(";")[0]


def test_web_masks_by_default_and_logs_unmask(app):
    base, out = app
    ben = _login(base, "ben", "staff password 1")
    amy = _login(base, "amy", "admin password 1")
    code, b, _ = _req(base, "/api/result?run=main-r1&unmask=1", ben)
    assert code == 200 and "張志豪" not in b.decode() and "張○豪" in b.decode()
    code, b, _ = _req(base, "/api/export?run=main-r1", ben)
    assert code == 200 and "張志豪" not in b.decode("utf-8") and "08807719" in b.decode("utf-8")
    code, b, _ = _req(base, "/api/inbox?set=main", ben)
    assert code == 200 and "林美玲" not in b.decode() and "陳大文" not in b.decode()
    # staff cannot unmask
    code, _, _ = _req(base, "/api/unmask", ben, {"run": "main-r1", "reason": "check the buyer"})
    assert code == 403
    # admin must give a reason
    code, _, _ = _req(base, "/api/unmask", amy, {"run": "main-r1", "reason": ""})
    assert code == 400
    # admin without a grant still sees masked data
    assert "張志豪" not in _req(base, "/api/result?run=main-r1&unmask=1", amy)[1].decode()
    code, _, _ = _req(base, "/api/unmask", amy, {"run": "main-r1", "reason": "client asked about invoice AB12345678"})
    assert code == 200
    assert "張志豪" in _req(base, "/api/result?run=main-r1&unmask=1", amy)[1].decode()
    assert "張志豪" not in _req(base, "/api/result?run=main-r1", amy)[1].decode()     # unmask is opt-in per request
    assert "張志豪" not in _req(base, "/api/result?run=main-r1&unmask=1", ben)[1].decode()  # the grant is amy's
    log = [json.loads(x) for x in (out / "oplog.jsonl").read_text(encoding="utf-8").splitlines()]
    um = [e for e in log if e["event"] == "unmask"]
    assert len(um) == 1 and um[0]["user"] == "amy" and um[0]["run_id"] == "main-r1" and "AB12345678" in um[0]["reason"]
    assert any(e["event"] == "denied" and e.get("path") == "/api/unmask" and e["user"] == "ben" for e in log)
    assert AuditLog.verify(out / "oplog.jsonl", KEY)[0]


def test_web_static_paths_do_not_bypass_masking(app):
    base, _ = app
    ben = _login(base, "ben", "staff password 1")
    for p in ("/out/runs/main-r1/result.json", "/out/runs/main-r1/export_media.csv", "/out/oplog.jsonl",
              "/img/main/clients.json", "/img/out/runs/main-r1/result.json", "/img/out/oplog.jsonl"):
        assert _req(base, p, ben)[0] == 404, p
    assert _req(base, "/out/runs/main-r1/crops/x.png", ben)[0] == 200
    assert _req(base, "/img/main/clients/C01/2026-09/IMG_1.jpg", ben)[0] == 200
