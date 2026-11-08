"""Web app review flow: decisions on held vouchers (saved, logged, applied to the export), one check per inbox,
interrupted checks listed and resumable, decisions carried to a new check, the inbox list, a bad image not stopping
a batch."""
import json

import pytest

from ledger_room import export, pipeline
from ledger_room.pipeline import AuditLog
from web import server
from tests.test_web_security import KEY, post, req

CLIENT = {"id": "C01", "name": "示範有限公司", "ubn": "12345675", "period": ["2026-09-01", "2026-10-31"]}


def _doc(name, inv):
    return {"doc_id": f"C01/2026-09/{name}", "client_id": "C01", "month": "2026-09", "image": f"heldout/clients/C01/2026-09/{name}",
            "fields": {"doc_type": "triplicate_invoice", "invoice_no": inv, "date": "2026-09-10", "seller_ubn": "04595257",
                       "buyer_ubn": "12345675", "net": 1000, "tax": 50, "total": 1050, "items": []},
            "provenance": {}, "boxes": {}, "timing": {}}


def _result():
    a, b = _doc("a.jpg", "AB12345678"), _doc("b.jpg", "AB12345678")
    ex = {"doc_id": b["doc_id"], "client_id": "C01", "rule_id": "DUP_CROSS_MONTH", "severity": "high", "title": "dup",
          "message": "dup", "citations": [], "related_docs": [a["doc_id"]], "evidence": [], "confidence": "high"}
    return {"summary": {"n_docs": 2}, "clients": [CLIENT], "docs": [a, b], "exceptions": [ex]}


@pytest.fixture
def web(tmp_path, monkeypatch):
    """The security tests' app fixture plus one inbox (heldout) and one finished run with a held voucher."""
    monkeypatch.setenv("LEDGER_SECRETS_DIR", str(tmp_path / "secrets"))
    data, out = tmp_path / "data", tmp_path / "data" / "out"
    box = data / "heldout"
    (box / "clients" / "C01" / "2026-09").mkdir(parents=True)
    for n in ("a.jpg", "b.jpg"):
        (box / "clients" / "C01" / "2026-09" / n).write_bytes(b"jpg")
    (box / "clients.json").write_text(json.dumps({"synthetic": True, "clients": [CLIENT]}), encoding="utf-8")
    (data / "private_x" / "clients").mkdir(parents=True)
    (data / "private_x" / "clients.json").write_text(json.dumps({"clients": []}), encoding="utf-8")
    run = out / "runs" / "heldout-20261108-100000-aaaa-live"
    run.mkdir(parents=True)
    (run / "result.json").write_text(json.dumps(_result(), ensure_ascii=False), encoding="utf-8")
    for mod in (server, pipeline):
        monkeypatch.setattr(mod, "DATA", data)
        monkeypatch.setattr(mod, "OUT", out)
    monkeypatch.setattr(server.keys, "audit_key", lambda create=True: KEY)
    monkeypatch.setattr(pipeline.keys, "audit_key", lambda create=True: KEY)
    monkeypatch.setattr(server, "OPLOG", AuditLog(out / "oplog.jsonl", key=KEY, append=True))
    for name, val in (("UNMASK", {}), ("FAILS", {}), ("IP_FAILS", {}), ("JOBS", {}), ("ACTIVE", {}), ("REVIEW_LOGS", {})):
        monkeypatch.setattr(server, name, val)
    for k, v in (("users", {}), ("local", True), ("tls", False), ("secure", False), ("all_inboxes", False)):
        monkeypatch.setitem(server.CFG, k, v)
    import threading
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
    port = srv.server_address[1]
    monkeypatch.setitem(server.CFG, "port", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port, out, run.name
    srv.shutdown()
    srv.server_close()


def _get(port, path):
    code, body, _ = req(port, "GET", path)
    return code, json.loads(body) if body[:1] in (b"{", b"[") else body


def test_inbox_list_shows_only_the_listed_inboxes_with_their_names(web, monkeypatch):
    port, _, _ = web
    code, boxes = _get(port, "/api/inboxes")
    assert code == 200 and [b["set"] for b in boxes] == ["heldout"]
    assert boxes[0]["name"]["zh"] and boxes[0]["synthetic"] is True
    assert _get(port, "/api/inbox?set=private_x")[0] == 400
    monkeypatch.setitem(server.CFG, "all_inboxes", True)
    assert {b["set"] for b in _get(port, "/api/inboxes")[1]} == {"heldout", "private_x"}


def test_decisions_are_saved_logged_and_applied_to_the_export(web):
    port, out, run = web
    b = "C01/2026-09/b.jpg"
    st = _get(port, f"/api/review?run={run}")[1]
    assert st["status"][b] == "HOLD" and st["decisions"] == {} and "DUP_CROSS_MONTH" in st["blocking"]
    assert post(port, "/api/review", {"run": run, "doc_id": b, "decision": "approve"})[0] == 400
    assert post(port, "/api/review", {"run": run, "doc_id": "C01/2026-09/zzz.jpg", "decision": "exclude"})[0] == 400
    assert post(port, "/api/review", {"run": "../x", "doc_id": b, "decision": "exclude"})[0] == 400
    assert post(port, "/api/review", {"run": run, "doc_id": b, "decision": "confirm"})[0] == 200
    assert post(port, "/api/review", {"run": run, "doc_id": b, "decision": "exclude", "note": "duplicate of a.jpg"})[0] == 200
    st = _get(port, f"/api/review?run={run}")[1]
    assert st["decisions"][b]["decision"] == "exclude" and st["decisions"][b]["user"] == "local" and st["log_ok"] is True
    code, csv_body, r = req(port, "GET", f"/api/export?run={run}")
    assert code == 200 and "media_reviewed" in r.getheader("Content-Disposition")
    lines = csv_body.decode("utf-8-sig").split("\r\n")
    head = lines[0].split(",")
    row_b = next(x for x in lines if "b.jpg" in x).split(",")
    assert row_b[head.index("status")] == "NOT_IN_FILE" and row_b[head.index("review_decision")] == "exclude"
    st = _get(port, f"/api/review?run={run}")[1]
    assert len(st["exports"]) == 1 and st["exports"][0]["counts"] == {"READY": 1, "NOT_IN_FILE": 1}
    assert post(port, "/api/review", {"run": run, "doc_id": b, "decision": "undo"})[0] == 200
    assert _get(port, f"/api/review?run={run}")[1]["decisions"] == {}
    ok, n = AuditLog.verify(out / "runs" / run / "review.jsonl", KEY)
    assert ok and n == 4


def test_apply_review_statuses():
    r = {"status": "HOLD", "status_label": "", "format_code_格式代號": "21", "deduction_code_扣抵代號": "1", "note": "x",
         "doc_id": "d"}
    assert export.apply_review(r, {"decision": "confirm", "user": "u"})["status"] == "READY"
    nd = export.apply_review(r, {"decision": "nondeductible", "user": "u"})
    assert nd["status"] == "READY" and nd["deduction_code_扣抵代號"] == "3"
    assert export.apply_review(r, {"decision": "defer", "user": "u"})["status"] == "NOT_IN_FILE"
    assert export.apply_review(r, None) is r
    no_code = dict(r, **{"format_code_格式代號": ""})
    assert export.apply_review(no_code, {"decision": "confirm"})["status"] == "NOT_IN_FILE"


def test_one_check_per_inbox_a_second_start_joins_the_first(web, monkeypatch):
    port, _, _ = web
    started = []
    monkeypatch.setattr(server, "run_job", lambda *a: started.append(a))
    code, body, _ = req(port, "POST", "/api/run", {"Content-Type": "application/json"}, json.dumps({"set": "heldout", "live": True}).encode())
    first = json.loads(body)
    assert code == 200 and first["status"] == "queued" and first["run_id"].endswith("-live") and "_done" not in first
    code, body, _ = req(port, "POST", "/api/run", {"Content-Type": "application/json"}, json.dumps({"set": "heldout", "live": True}).encode())
    assert code == 409 and json.loads(body)["job"]["id"] == first["id"]
    assert _get(port, "/api/jobs?set=heldout")[1]["active"]["id"] == first["id"]


def test_an_interrupted_check_is_listed_and_resumes_with_its_reads(web, monkeypatch):
    port, out, _ = web
    rid = "heldout-20261108-110000-bbbb-live"
    (out / "runs" / rid).mkdir()
    (out / "runs" / rid / "job.json").write_text(json.dumps(
        {"id": "x", "set": "heldout", "status": "reading", "i": 1, "n": 2, "run_id": rid, "live": True,
         "done": ["C01/2026-09/a.jpg"]}), encoding="utf-8")
    jobs = _get(port, "/api/jobs?set=heldout")[1]
    assert jobs["active"] is None and jobs["interrupted"][0]["run_id"] == rid and jobs["interrupted"][0]["n_done"] == 1
    started = []
    monkeypatch.setattr(server, "run_job", lambda *a: started.append(a))
    assert post(port, "/api/run", {"set": "heldout", "resume": "heldout-20261108-999999-cccc-live"})[0] == 400
    code, body, _ = post(port, "/api/run", {"set": "heldout", "resume": rid})
    assert code == 200 and json.loads(body)["resumed_from"] == rid
    import time
    for _ in range(50):
        if started:
            break
        time.sleep(0.02)
    assert started[0][4] == {"C01/2026-09/a.jpg"} and started[0][5] == rid
    assert json.loads((out / "runs" / rid / "job.json").read_text(encoding="utf-8"))["status"] == "resumed"
    assert _get(port, "/api/jobs?set=heldout")[1]["interrupted"] == []


def test_decisions_carry_over_to_a_new_check_only_when_the_issues_are_the_same(web):
    port, out, run = web
    b = "C01/2026-09/b.jpg"
    assert post(port, "/api/review", {"run": run, "doc_id": b, "decision": "exclude"})[0] == 200
    same = _result()
    assert server.carry_over_decisions("heldout", "heldout-20261108-120000-dddd-live", same) == 1
    dec, _ = server.review_state("heldout-20261108-120000-dddd-live")
    assert dec[b]["decision"] == "exclude"
    changed = _result()
    changed["exceptions"][0]["rule_id"] = "UBN_CHECKSUM"
    assert server.carry_over_decisions("heldout", "heldout-20261108-130000-eeee-live", changed) == 0


class _Reader:
    model_id, device = "fake", "CPU"

    def read_raw(self, path):
        if path.name == "bad.jpg":
            raise OSError("cannot identify image file")
        return {"timing": {"total_s": 0.01}}

    def read(self, path, raw):
        return {"fields": {"doc_type": "receipt", "total": 100, "items": []}, "provenance": {}, "boxes": {},
                "timing": raw["timing"], "image_size": [8, 8], "ocr_lines": []}


def test_one_bad_image_goes_to_a_person_and_the_batch_goes_on(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "OUT", tmp_path / "out")
    docs = []
    for n in ("ok.jpg", "bad.jpg", "ok2.jpg"):
        p = tmp_path / n
        p.write_bytes(n.encode())
        docs.append({"doc_id": f"C01/2026-09/{n}", "path": p})
    with pytest.raises(OSError):
        pipeline.read_docs(docs, _Reader(), cache=False)
    reads = pipeline.read_docs(docs, _Reader(), cache=False, on_error="hold")
    assert reads["C01/2026-09/bad.jpg"]["read_failed"]["reason"] == "read_error"
    assert "read_failed" not in reads["C01/2026-09/ok2.jpg"]
    rows = export.rows({"clients": [CLIENT], "exceptions": [], "docs": [
        {"doc_id": "C01/2026-09/bad.jpg", "client_id": "C01", "fields": {}, "read_failed": {"reason": "read_error"}}]})
    assert rows[0]["status"] == "HOLD"
