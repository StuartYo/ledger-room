"""A reviewer corrects a field the reader got wrong: the value is checked, logged in the signed review log and the
operation log, the machine reading is kept, the rules run again on the corrected value, and the export uses it."""
import json

import pytest

from ledger_room import corrections, rules
from ledger_room.pipeline import AuditLog
from tests.test_web_review import CLIENT, web  # noqa: F401  (fixture)
from tests.test_web_security import KEY, post, req


def _misread_doc():
    # the reader took the tax (222) for the total and found neither net nor tax: amounts cannot be checked
    return {"doc_id": "C01/2026-09/a.jpg", "client_id": "C01", "month": "2026-09", "image": "heldout/clients/C01/2026-09/a.jpg",
            "fields": {"doc_type": "triplicate_invoice", "invoice_no": "AB12345678", "date": "2026-09-14", "seller_ubn": "04595257",
                       "buyer_ubn": "12345675", "net": None, "tax": None, "total": 222, "items": []},
            "provenance": {"invoice_no": "ocr-confirmed", "date": "ocr-confirmed", "seller_ubn": "ocr-confirmed",
                           "buyer_ubn": "ocr-confirmed", "total": "ocr-total-line"},
            "boxes": {"total": [10, 10, 60, 30]}, "image_size": [100, 100], "timing": {}}


def _result():
    d = _misread_doc()
    ex = {"doc_id": d["doc_id"], "client_id": "C01", "rule_id": "AMOUNT_MISMATCH", "severity": "hold", "title": "amounts",
          "message": "Net or tax not read", "evidence_fields": ["net", "tax", "total"], "citations": [], "related_docs": [],
          "evidence": [{"field": "total", "value": 222, "box": [0, 0, 70, 40], "crop": "x.png", "provenance": "ocr-total-line"}],
          "confidence": "high"}
    return {"summary": {"n_docs": 1, "n_exceptions": 1}, "clients": [CLIENT], "docs": [d], "exceptions": [ex]}


def test_normalize_accepts_the_usual_ways_of_writing_a_value():
    n = corrections.normalize
    assert n("total", "4,652") == 4652 and n("total", "NT$4,652") == 4652 and n("net", " 4430 ") == 4430
    assert n("invoice_no", "mb-43283260") == "MB43283260"
    assert n("date", "115/09/14") == "2026-09-14" == n("date", "2026-9-14") == n("date", "1150914") == n("date", "115年9月14日")
    assert n("seller_ubn", "0459 5257") == "04595257"
    assert n("tax", "") is None
    for f, v in (("total", "4.5"), ("total", "-3"), ("invoice_no", "M43283260"), ("seller_ubn", "1234567"),
                 ("date", "2026-02-30"), ("date", "yesterday"), ("seller_name", "x")):
        with pytest.raises(corrections.Invalid):
            n(f, v)


def test_state_keeps_the_latest_value_and_a_correction_back_to_the_reading_removes_it():
    e = lambda f, old, new: {"event": "correct", "doc_id": "d", "field": f, "old": old, "new": new, "user": "u"}  # noqa: E731
    assert corrections.state([e("total", 222, 4652)])["d"]["total"]["new"] == 4652
    assert corrections.state([e("total", 222, 4000), e("total", 222, 4652)])["d"]["total"]["new"] == 4652
    assert corrections.state([e("total", 222, 4652), e("total", 222, 222)]) == {}
    assert corrections.state([{"event": "review", "doc_id": "d", "decision": "confirm"}]) == {}


def test_apply_rechecks_the_rules_on_the_corrected_values():
    res = _result()
    fixed = corrections.apply(res, {"C01/2026-09/a.jpg": {
        "net": {"old": None, "new": 4430}, "tax": {"old": None, "new": 222}, "total": {"old": 222, "new": 4652}}})
    d = fixed["docs"][0]
    assert d["fields"]["total"] == 4652 and d["provenance"]["total"] == corrections.PERSON
    assert set(d["corrections"]) == {"net", "tax", "total"}
    assert fixed["exceptions"] == []                         # net + tax = total now: nothing left to hold
    assert res["docs"][0]["fields"]["total"] == 222          # the machine reading is not touched
    # a wrong total typed in: the arithmetic rule holds it again, and says why
    bad = corrections.apply(res, {"C01/2026-09/a.jpg": {
        "net": {"old": None, "new": 4430}, "tax": {"old": None, "new": 222}, "total": {"old": 222, "new": 4600}}})
    assert [e["rule_id"] for e in bad["exceptions"]] == ["AMOUNT_MISMATCH"]
    assert "4,600" in bad["exceptions"][0]["message"] and bad["exceptions"][0]["recheck"] is True
    # a seller tax ID with a wrong check digit: the checksum rule runs on the typed value
    ubn = corrections.apply(res, {"C01/2026-09/a.jpg": {"seller_ubn": {"old": "04595257", "new": "04595258"}}})
    assert "UBN_CHECKSUM" in {e["rule_id"] for e in ubn["exceptions"]}


def test_a_value_typed_by_a_person_counts_as_confirmed_in_strict_mode(monkeypatch):
    from ledger_room import pipeline
    monkeypatch.setattr(pipeline, "STRICT", True)
    d = _misread_doc()
    d["provenance"]["invoice_no"] = "ocr"        # one source: strict reading sends it to a person
    res = {"summary": {}, "clients": [CLIENT], "docs": [d], "exceptions": [
        {"doc_id": d["doc_id"], "client_id": "C01", "rule_id": "READ_REVIEW", "severity": "review", "message": "x",
         "evidence_fields": ["invoice_no"], "citations": [], "related_docs": [], "evidence": []}]}
    rd = pipeline.to_rule_doc(d, d["fields"], d["provenance"])
    assert "invoice_no" in rd.uncertain
    fixed = corrections.apply(res, {d["doc_id"]: {"invoice_no": {"old": "AB12345678", "new": "AB12345679"}}})
    assert not any(e["rule_id"] == "READ_REVIEW" and e["evidence_fields"] == ["invoice_no"] for e in fixed["exceptions"])
    assert rules.norm_invoice_no(fixed["docs"][0]["fields"]["invoice_no"]) == "AB12345679"


def _get(port, path):
    code, body, _ = req(port, "GET", path)
    return code, json.loads(body)


def test_web_correction_is_logged_rechecked_exported_and_can_be_undone(web):  # noqa: F811
    port, out, run = web
    rdir = out / "runs" / run
    (rdir / "result.json").write_text(json.dumps(_result(), ensure_ascii=False), encoding="utf-8")
    doc = "C01/2026-09/a.jpg"
    assert _get(port, f"/api/review?run={run}")[1]["status"][doc] == "HOLD"
    # refused: unknown field, a value that is not an amount, unknown document, bad run
    assert post(port, "/api/correct", {"run": run, "doc_id": doc, "field": "seller_name", "value": "x"})[0] == 400
    code, body, _ = post(port, "/api/correct", {"run": run, "doc_id": doc, "field": "total", "value": "abc"})
    assert code == 400 and json.loads(body)["code"] == "amount"
    assert post(port, "/api/correct", {"run": run, "doc_id": "C01/2026-09/zz.jpg", "field": "total", "value": "1"})[0] == 400
    assert post(port, "/api/correct", {"run": "../x", "doc_id": doc, "field": "total", "value": "1"})[0] == 400
    for f, v in (("total", "4,652"), ("net", "4430")):
        code, body, _ = post(port, "/api/correct", {"run": run, "doc_id": doc, "field": f, "value": v})
        assert code == 200
    r = json.loads(body)
    assert r["status"] == "HOLD" and [e["rule_id"] for e in r["exceptions"]] == ["AMOUNT_MISMATCH"]  # tax still missing
    code, body, _ = post(port, "/api/correct", {"run": run, "doc_id": doc, "field": "tax", "value": "222"})
    r = json.loads(body)
    assert code == 200 and r["exceptions"] == [] and r["status"] == "READY" and r["machine"] is None
    # the same value again changes nothing and logs nothing
    assert json.loads(post(port, "/api/correct", {"run": run, "doc_id": doc, "field": "tax", "value": "222"})[1])["changed"] is False
    res = _get(port, f"/api/result?run={run}")[1]
    d = res["docs"][0]
    assert d["fields"]["total"] == 4652 and d["corrections"]["total"] == {**d["corrections"]["total"], "old": 222, "new": 4652}
    assert res["exceptions"] == []
    st = _get(port, f"/api/review?run={run}")[1]
    assert st["status"][doc] == "READY" and st["corrections"][doc]["total"]["user"] == "local" and st["log_ok"] is True
    code, csv_body, _ = req(port, "GET", f"/api/export?run={run}")
    lines = csv_body.decode("utf-8-sig").split("\r\n")
    head, row = lines[0].split(","), next(x for x in lines if "a.jpg" in x).split(",")
    assert row[head.index("total")] == "4652" and row[head.index("tax")] == "222" and row[head.index("status")] == "READY"
    assert "total corrected by local: read 222, paper 4652" in next(x for x in lines if "a.jpg" in x)
    # the review log: three corrections, one export, hash chain and signatures verify; the old value is kept
    ok, n = AuditLog.verify(rdir / "review.jsonl", KEY)
    assert ok and n == 4
    ent = [json.loads(x) for x in AuditLog.lines(rdir / "review.jsonl")]
    assert [e["event"] for e in ent] == ["correct", "correct", "correct", "export"]
    assert ent[0]["old"] == 222 and ent[0]["new"] == 4652 and ent[0]["previous"] == 222
    assert any(e.get("event") == "correct" and e.get("field") == "total" for e in
               (json.loads(x) for x in AuditLog.lines(out / "oplog.jsonl")))
    # undo: back to the machine reading; the entry stays in the log, the field is no longer corrected
    r = json.loads(post(port, "/api/correct", {"run": run, "doc_id": doc, "field": "total", "revert": True})[1])
    assert r["value"] == 222 and "total" not in r["corrections"]
    assert _get(port, f"/api/result?run={run}")[1]["docs"][0]["fields"]["total"] == 222
    assert AuditLog.verify(rdir / "review.jsonl", KEY) == (True, 5)
    assert json.loads((rdir / "result.json").read_text(encoding="utf-8"))["docs"][0]["fields"]["total"] == 222
