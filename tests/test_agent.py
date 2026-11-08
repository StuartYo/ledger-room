"""The review agent (ledger_room/agent.py) with a scripted stand-in for the 27B: the tool loop, the code check (v3), the
pipeline step that clears a document only when code verified the fix, the audit-log entries, replay from stored runs,
and a person overriding the agent in the web app.

The five wrong values the 27B proposed in the 2026-11-09 experiment (G_代理實驗.md) are here as cases that code
must refuse, every time."""
import json

import pytest
from PIL import Image

from ledger_room import agent as A
from ledger_room import pipeline, rules
from ledger_room.ocr import Line
from ledger_room.pipeline import AuditLog

CLIENT = {"id": "C01", "name": "示範有限公司", "ubn": "12345675", "period": ["2026-09-01", "2026-10-31"]}


class FakeModel:
    """Replies in order; records the prompts it was given."""
    model_id, device, load_s = "fake-27b", "GPU.1", 0.1

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def generate(self, prompt, image, max_new_tokens=320):
        self.prompts.append(prompt)
        return (self.replies.pop(0) if self.replies else '{"tool":"finish","fixes":{},"need_human":[]}'), {"vlm_s": 0.01}


def ocr_line(i, text, y=10):
    return {"id": f"L{i}", "text": text, "box": [10, y, 300, y + 20], "conf": 0.99}


def read_of(fields, prov, lines, raw=None):
    return {"fields": fields, "provenance": prov, "boxes": {}, "ocr_lines": lines,
            "raw": raw if raw is not None else json.dumps({"no": fields.get("invoice_no"), "total": fields.get("total")}),
            "timing": {}, "image_size": [400, 300]}


def triplicate(**kw):
    f = {"doc_type": "triplicate_invoice", "invoice_no": "AB12345678", "date": "2026-09-10", "seller_ubn": "04595257",
         "buyer_ubn": "12345675", "net": 1000, "tax": 50, "total": 1050, "items": []}
    f.update(kw)
    return f


GOOD = {"invoice_no": "ocr-confirmed", "date": "ocr-confirmed", "seller_ubn": "ocr-confirmed",
        "buyer_ubn": "ocr-confirmed", "net": "ocr-confirmed", "tax": "ocr-confirmed", "total": "ocr-confirmed"}


def check(proposal: dict, read: dict, evidence=None, fixable=None):
    ag = {"final": {"fixes": proposal}, "fixable": fixable or sorted(proposal)}
    return A.verify(ag, read, evidence or {})


# ---------------------------------------------------------------- the code check (v3) --------------------------------
def test_the_five_wrong_values_from_the_experiment_are_all_refused():
    # 1. W01/IMG_8179: buyer UBN with a digit missing (truth 00668398)
    r = read_of(triplicate(buyer_ubn=None), {**GOOD, "buyer_ubn": "missing"}, [ocr_line(1, "買方統一編號")])
    v = check({"buyer_ubn": "0668398"}, r, {"crop_texts": ["0668398"]})
    assert v["accepted"] == {} and v["fields"]["buyer_ubn"]["why"] == "format"
    # 2-3. W03/IMG_2395: a receipt with no net and no tax; the agent filled net 190 and tax 0 (truth: empty)
    r = read_of({"doc_type": "receipt", "invoice_no": None, "net": None, "tax": None, "total": None},
                {"total": "missing"}, [ocr_line(1, "收據"), ocr_line(2, "190", 40)])
    v = check({"net": 190, "tax": 0, "total": 190}, r, {"crop_texts": ["190"]}, fixable=["net", "tax", "total"])
    assert v["accepted"] == {}
    assert not v["fields"]["net"]["accepted"] and not v["fields"]["tax"]["accepted"]
    # 4. W04/IMG_4741: the agent took the net (6440, printed on the page) for the total (truth 6762). v1 accepted it
    #    because 6440 is on the page; v3 needs net + tax = total with the other two confirmed or printed.
    r = read_of(triplicate(net=6440, tax=322, total=None), {**GOOD, "tax": "model-only", "total": "missing"},
                [ocr_line(1, "銷售額 6440"), ocr_line(2, "營業稅", 40)])
    v = check({"total": 6440}, r, {"crop_texts": ["6440"]}, fixable=["net", "tax", "total"])
    assert v["accepted"] == {} and v["fields"]["total"]["evidence"] == []
    # 5. W04/IMG_9233: seller UBN with a bad check digit (truth 62436671)
    r = read_of(triplicate(seller_ubn=None), {**GOOD, "seller_ubn": "missing"}, [ocr_line(1, "賣方 99033033")])
    v = check({"seller_ubn": "99033033"}, r, {"crop_texts": ["99033033"]})
    assert v["accepted"] == {} and v["fields"]["seller_ubn"]["why"] == "format"


def test_an_amount_is_never_evidenced_by_its_digits_on_the_page_alone():
    # 6440 is printed (the net) and the tax is confirmed, but 6440 + 322 != 6440: refused even with crop OCR
    r = read_of(triplicate(net=6440, tax=322, total=None), {**GOOD, "total": "missing"},
                [ocr_line(1, "6440"), ocr_line(2, "322", 40)])
    assert check({"total": 6440}, r, {"crop_texts": ["總計 6440"]}, fixable=["net", "tax", "total"])["accepted"] == {}
    # the right total follows from the confirmed net and tax, even when the total itself is unreadable
    v = check({"total": 6762}, r, {"crop_texts": []}, fixable=["net", "tax", "total"])
    assert v["accepted"] == {"total": 6762} and v["fields"]["total"]["evidence"] == ["arithmetic"]


def test_arithmetic_needs_the_other_two_numbers_confirmed_or_printed():
    # net and tax only the model read (not on the page): the sum proves nothing (blind3 IMG_20260808_181508 case)
    r = read_of(triplicate(net=3070, tax=154, total=None),
                {**GOOD, "net": "model-only", "tax": "model-only", "total": "missing"}, [ocr_line(1, "發票")])
    assert check({"total": 3224}, r, fixable=["net", "tax", "total"])["accepted"] == {}
    # the same numbers printed on the page: accepted
    r["ocr_lines"] = [ocr_line(1, "3070"), ocr_line(2, "154", 40)]
    assert check({"total": 3224}, r, fixable=["net", "tax", "total"])["accepted"] == {"total": 3224}


def test_ubn_needs_a_non_model_source_and_the_right_party():
    r = read_of(triplicate(buyer_ubn=None), {**GOOD, "buyer_ubn": "missing"}, [ocr_line(1, "買方統一編號")])
    assert check({"buyer_ubn": "00668398"}, r)["accepted"] == {}                         # only the model said so
    v = check({"buyer_ubn": "00668398"}, r, {"crop_texts": ["統一編號 00668398"]})          # the zoom crop's OCR read it
    assert v["accepted"] == {"buyer_ubn": "00668398"} and v["fields"]["buyer_ubn"]["evidence"] == ["crop-ocr"]
    # OCR puts 42017674 with the buyer: refused as the seller's number
    r = read_of(triplicate(seller_ubn=None), {**GOOD, "seller_ubn": "missing"}, [ocr_line(1, "買方：42017674")])
    v = check({"seller_ubn": "42017674"}, r, {"crop_texts": ["42017674"]})
    assert v["accepted"] == {} and v["fields"]["seller_ubn"]["why"].startswith("ocr-other-party")
    # the buyer's number proposed as the seller's
    r = read_of(triplicate(seller_ubn=None), {**GOOD, "seller_ubn": "missing"}, [ocr_line(1, "統一編號")])
    assert check({"seller_ubn": "12345675"}, r, {"crop_texts": ["12345675"]})["accepted"] == {}


def test_amounts_must_add_up_after_the_fix():
    r = read_of(triplicate(net=1000, tax=50, total=None), {**GOOD, "total": "missing"}, [ocr_line(1, "1000 50")])
    v = check({"total": 1050, "tax": 60}, r, {"crop_texts": ["60"]}, fixable=["net", "tax", "total"])
    assert "total" not in v["accepted"] and "tax" not in v["accepted"]


# ---------------------------------------------------------------- the tool loop ---------------------------------------
@pytest.fixture
def page(tmp_path):
    p = tmp_path / "a.jpg"
    Image.new("RGB", (400, 300), "white").save(p)
    return {"doc_id": "C01/2026-09/a.jpg", "client_id": "C01", "month": "2026-09", "path": p}


def test_tool_loop_records_every_call_and_stops_at_six(page):
    r = read_of(triplicate(total=None), {**GOOD, "total": "missing"}, [ocr_line(1, "1000"), ocr_line(2, "50", 40)])
    ex = [rules.Exception_(page["doc_id"], "C01", "READ_REVIEW", "review", "Total not found", ["total"])]
    m = FakeModel(['{"tool":"zoom","box":[0,0,200,100]}', '{"tool":"calc","net":1000,"tax":50,"total":1050}',
                   '{"tool":"ubn_check","ubn":"04595257"}',
                   '{"tool":"finish","fixes":{"total":1050},"need_human":[],"note":"sum of net and tax"}'])
    tools = A.Tools(page, r, {"C01": CLIENT}, {page["doc_id"]: r}, [page],
                    ocr=lambda arr: [Line("z1", "總計 1050", (0, 0, 10, 10), 0.9)])
    run = A.run_agent(m, tools, r, ex)
    assert [s["tool"] for s in run["steps"]] == ["zoom", "calc", "ubn_check", "finish"]
    assert "總計 1050" in run["steps"][0]["result"] and "consistent" in run["steps"][1]["result"]
    assert tools.evidence()["crop_texts"] == ["總計 1050"]
    assert run["fixable"] == ["net", "tax", "total"] and "Total not found" in m.prompts[0]
    # a model that never finishes is stopped after six tool calls and leaves the fields to a person
    m = FakeModel(['{"tool":"calc","net":1,"tax":0,"total":1}'] * 20)
    run = A.run_agent(m, A.Tools(page, r, {"C01": CLIENT}, {page["doc_id"]: r}, [page]), r, ex)
    assert len(run["steps"]) == 7 and run["final"]["need_human"] == ["net", "tax", "total"]
    assert "used all your steps" in m.prompts[-1]


def test_unreadable_replies_end_with_a_person(page):
    r = read_of(triplicate(total=None), {**GOOD, "total": "missing"}, [])
    ex = [rules.Exception_(page["doc_id"], "C01", "READ_REVIEW", "review", "x", ["total"])]
    run = A.run_agent(FakeModel(["I think it is 1050", "sure"]), A.Tools(page, r, {}, {}, [page]), r, ex)
    assert run["final"]["need_human"] == ["net", "tax", "total"] and run["final"]["fixes"] == {}


# ---------------------------------------------------------------- the pipeline step -----------------------------------
@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_SECRETS_DIR", str(tmp_path / "secrets"))
    data = tmp_path / "data"
    d = data / "demo" / "clients" / "C01" / "2026-09"
    d.mkdir(parents=True)
    for n in ("a.jpg", "b.jpg", "c.jpg"):
        Image.new("RGB", (400, 300), "white").save(d / n)
    (data / "demo" / "clients.json").write_text(json.dumps({"synthetic": True, "clients": [CLIENT]}), encoding="utf-8")
    monkeypatch.setattr(pipeline, "DATA", data)
    monkeypatch.setattr(pipeline, "OUT", data / "out")
    monkeypatch.setattr(pipeline.keys, "audit_key", lambda create=True: b"k" * 32)
    monkeypatch.setattr(pipeline, "STRICT", False)
    reads = {
        # total covered; net and tax printed and confirmed: the agent can clear it by arithmetic
        "C01/2026-09/a.jpg": read_of(triplicate(invoice_no="AB12345678", total=1150, net=1100, tax=55),
                                     {**GOOD, "total": "model-only"}, [ocr_line(1, "1100"), ocr_line(2, "55", 40)]),
        # total covered and net, tax read by the model only: the agent's total cannot be checked -> a person
        "C01/2026-09/b.jpg": read_of(triplicate(invoice_no="AB12345679", total=2100, net=2000, tax=100),
                                     {**GOOD, "net": "model-only", "tax": "model-only", "total": "model-only"},
                                     [ocr_line(1, "發票")]),
        "C01/2026-09/c.jpg": read_of(triplicate(invoice_no="AB12345670"), GOOD, [ocr_line(1, "1000 50 1050")]),
    }

    def read_fn(docs, reader, **kw):
        return {d["doc_id"]: {**reads[d["doc_id"]], "sha256": d["doc_id"].replace("/", "_") * 2} for d in docs}
    return data, read_fn


def scripted():
    return FakeModel(['{"tool":"zoom","box":[0,0,100,50]}', '{"tool":"finish","fixes":{"total":1155}}',
                      '{"tool":"finish","fixes":{"total":2100},"note":"covered"}'])


def test_pipeline_clears_only_what_code_verified_and_logs_it(inbox, monkeypatch):
    data, read_fn = inbox
    monkeypatch.setattr(A.Tools, "zoom", lambda self, box: (self.crop_texts.append("(nothing read)"), "zoomed", None)[1:])
    ag = A.Agent(scripted())
    res = pipeline.audit("demo", None, read_fn=read_fn, agent=ag, run_id="demo-1")
    docs = {d["doc_id"]: d for d in res["docs"]}
    a, b, c = (docs[f"C01/2026-09/{n}.jpg"] for n in "abc")
    assert a["agent"]["status"] == "verified" and a["fields"]["total"] == 1155
    assert a["provenance"]["total"] == "agent-verified" and a["agent"]["machine"] == {"total": 1150}
    # the original reasons are kept: total read by the model only, and net + tax != total
    assert sorted(e["rule_id"] for e in a["agent"]["held_exceptions"]) == ["AMOUNT_MISMATCH", "READ_REVIEW"]
    assert [s["tool"] for s in a["agent"]["steps"]] == ["zoom", "finish"]
    assert a["agent"]["checks"]["total"]["evidence"] == ["arithmetic"]
    assert b["agent"]["status"] == "held" and b["fields"]["total"] == 2100 and b["agent"]["accepted"] == {}
    assert "agent" not in c
    held = {e["doc_id"] for e in res["exceptions"] if e["severity"] in ("hold", "review")}
    assert held == {b["doc_id"]}
    s = res["summary"]["agent"]
    assert s["ran"] and s["tried"] == 2 and s["verified"] == 1 and s["model"] == "fake-27b"
    log = data / "out" / "runs" / "demo-1" / "audit_log.jsonl"
    assert AuditLog.verify(log, b"k" * 32)[0]
    ent = [json.loads(x) for x in AuditLog.lines(log)]
    chk = [e for e in ent if e["event"] == "agent_check"]
    assert [e["doc_id"] for e in chk] == [a["doc_id"], b["doc_id"]]
    assert chk[0]["tools"][0]["tool"] == "zoom" and chk[0]["checks"]["total"]["accepted"] is True
    assert chk[0]["status"] == "verified" and chk[1]["status"] == "held"
    # the export files the verified value
    from ledger_room import export
    row = next(r for r in export.rows(res) if r["doc_id"] == a["doc_id"])
    assert row["status"] == "READY" and row["total"] == 1155 and "agent" in row["note"]


def test_agent_progress_counts_the_held_vouchers_it_looks_at(inbox, monkeypatch):
    # the web page shows "the agent is on the held vouchers: i of n" instead of standing at "3 of 3"
    data, read_fn = inbox
    monkeypatch.setattr(A.Tools, "zoom", lambda self, box: (self.crop_texts.append("x"), "zoomed", None)[1:])
    calls = []
    pipeline.audit("demo", None, read_fn=read_fn, agent=A.Agent(scripted()), run_id="demo-p",
                   agent_progress=lambda i, n, doc_id, rec: calls.append((i, n, doc_id)))
    assert calls == [(0, 2, "C01/2026-09/a.jpg"), (1, 2, "C01/2026-09/b.jpg"), (2, 2, None)]


def test_no_agent_on_this_machine_runs_no_agent_step(inbox):
    data, read_fn = inbox
    res = pipeline.audit("demo", None, read_fn=read_fn, run_id="demo-2")
    assert res["summary"]["agent"]["ran"] is False and not any("agent" in d for d in res["docs"])
    assert {e["doc_id"] for e in res["exceptions"]} >= {"C01/2026-09/a.jpg", "C01/2026-09/b.jpg"}


def test_replay_uses_the_stored_runs_and_checks_them_again(inbox, monkeypatch):
    data, read_fn = inbox
    monkeypatch.setattr(A.Tools, "zoom", lambda self, box: (self.crop_texts.append("x"), "zoomed", None)[1:])
    pipeline.audit("demo", None, read_fn=read_fn, agent=A.Agent(scripted()), run_id="demo-3")
    res = pipeline.audit("demo", None, read_fn=read_fn, agent_replay=True, run_id="demo-4")
    a = next(d for d in res["docs"] if d["doc_id"] == "C01/2026-09/a.jpg")
    assert a["agent"]["status"] == "verified" and a["agent"]["replayed"] is True and a["fields"]["total"] == 1155
    assert res["summary"]["agent"]["replay"] is True and res["summary"]["agent"]["replayed"] == 2
    # a stored run is not used for a different first reading
    def other(docs, reader, **kw):
        out = read_fn(docs, reader)
        out["C01/2026-09/a.jpg"]["fields"] = {**out["C01/2026-09/a.jpg"]["fields"], "net": 1101}
        return out
    res = pipeline.audit("demo", None, read_fn=other, agent_replay=True, run_id="demo-5")
    assert "agent" not in next(d for d in res["docs"] if d["doc_id"] == "C01/2026-09/a.jpg")


def test_availability_without_a_model_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("LEDGER_AGENT_MODEL_DIR", str(tmp_path / "none"))
    assert A.availability()["available"] is False and A.availability()["reason"] == "no_model"
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "openvino_language_model.xml").write_text("x")
    monkeypatch.setenv("LEDGER_AGENT_MODEL_DIR", str(tmp_path / "m"))
    monkeypatch.setenv("LEDGER_AGENT_DEVICE", "GPU.1")
    av = A.availability()
    assert av["available"] and av["device"] == "GPU.1" and av["model"] == "m"
    monkeypatch.setenv("LEDGER_AGENT", "0")
    assert A.availability() == {**av, "available": False, "reason": "off"}


# ---------------------------------------------------------------- a person overrides the agent (web) ------------------
def _agent_result():
    d = {"doc_id": "C01/2026-09/a.jpg", "client_id": "C01", "month": "2026-09", "image": "heldout/clients/C01/2026-09/a.jpg",
         "fields": triplicate(total=1150, net=1100, tax=50 + 5), "provenance": {**GOOD, "total": "agent-verified"},
         "boxes": {}, "image_size": [400, 300], "timing": {}}
    held = {"doc_id": d["doc_id"], "client_id": "C01", "rule_id": "READ_REVIEW", "severity": "review", "title": "read",
            "message": "Total not confirmed on the page (read NT$1,155); check it by eye.", "evidence_fields": ["total"],
            "citations": [], "related_docs": [], "evidence": [], "confidence": "low"}
    d["agent"] = {"status": "verified", "model": "Qwen3.5-27B-int4-ov", "device": "GPU.1", "accepted": {"total": 1150},
                  "machine": {"total": 1155}, "machine_prov": {"total": "model-only"}, "held_exceptions": [held],
                  "held_for": [{k: held[k] for k in ("rule_id", "severity", "message", "evidence_fields")}],
                  "steps": [{"step": 1, "tool": "finish", "args": {"fixes": {"total": 1150}}}],
                  "checks": {"total": {"value": 1150, "old": 1155, "accepted": True, "evidence": ["arithmetic"], "why": "ok"}}}
    return {"summary": {"n_docs": 1, "n_exceptions": 0, "agent": {"ran": True}}, "clients": [CLIENT], "docs": [d],
            "exceptions": []}


def test_a_person_can_reject_the_agent_fix_and_take_that_back(web):  # noqa: F811
    from tests.test_web_security import post, req
    port, out, run = web
    rdir = out / "runs" / run
    (rdir / "result.json").write_text(json.dumps(_agent_result(), ensure_ascii=False), encoding="utf-8")
    doc = "C01/2026-09/a.jpg"
    get = lambda p: json.loads(req(port, "GET", p)[1])  # noqa: E731
    assert get(f"/api/review?run={run}")["status"][doc] == "READY"
    st = get("/api/status")["agent"]
    assert set(st) >= {"available", "loaded", "model", "device", "reason"} and st["loaded"] is False
    # refused: unknown action, a document without an agent fix
    assert post(port, "/api/agent-override", {"run": run, "doc_id": doc, "action": "maybe"})[0] == 400
    assert post(port, "/api/agent-override", {"run": run, "doc_id": "C01/2026-09/b.jpg", "action": "reject"})[0] == 400
    code, body, _ = post(port, "/api/agent-override", {"run": run, "doc_id": doc, "action": "reject", "note": "paper says 1155"})
    r = json.loads(body)
    assert code == 200 and r["changed"] and [e["rule_id"] for e in r["exceptions"]] == ["READ_REVIEW"]
    res = get(f"/api/result?run={run}")
    d = res["docs"][0]
    assert d["fields"]["total"] == 1155 and d["provenance"]["total"] == "model-only" and d["agent"]["overridden"]["user"] == "local"
    rv = get(f"/api/review?run={run}")
    assert rv["status"][doc] == "HOLD" and doc in rv["agent_overrides"] and rv["log_ok"] is True
    # the same again changes nothing; restore puts the agent's value back
    assert json.loads(post(port, "/api/agent-override", {"run": run, "doc_id": doc, "action": "reject"})[1])["changed"] is False
    assert json.loads(post(port, "/api/agent-override", {"run": run, "doc_id": doc, "action": "restore"})[1])["exceptions"] == []
    assert get(f"/api/result?run={run}")["docs"][0]["fields"]["total"] == 1150
    ent = [json.loads(x) for x in AuditLog.lines(rdir / "review.jsonl")]
    assert [(e["event"], e["action"]) for e in ent] == [("agent_override", "reject"), ("agent_override", "restore")]
    assert ent[0]["note"] == "paper says 1155" and ent[0]["machine"] == {"total": 1155}
    ops = [json.loads(x) for x in AuditLog.lines(out / "oplog.jsonl")]
    assert [e["action"] for e in ops if e["event"] == "agent_override"] == ["reject", "restore"]
    # changing the agent's value by hand is an override too, and the log says so
    code, body, _ = post(port, "/api/correct", {"run": run, "doc_id": doc, "field": "total", "value": "1155"})
    assert code == 200
    last = json.loads(AuditLog.lines(rdir / "review.jsonl")[-1])
    assert last["event"] == "correct" and last["over_agent"] is True and last["old"] == 1150 and last["machine"] == 1155


from tests.test_web_review import web  # noqa: E402,F401  (fixture)


def test_history_and_platform_count_as_independent_sources():
    # never reached on the synthetic sets (no firm history, no platform download there): checked here directly
    read = read_of(triplicate(seller_ubn=None, invoice_no="AB12345678"), {**GOOD, "seller_ubn": "missing",
                   "invoice_no": "model-only"}, [ocr_line(1, "統一發票")])
    got = check({"seller_ubn": "04595257"}, read, {"history_seller_ubn": ["04595257"]})
    assert got["fields"]["seller_ubn"]["accepted"] and got["fields"]["seller_ubn"]["evidence"] == ["history"]
    got = check({"invoice_no": "AB12345678"}, read, {"platform_hits": [{"invoice_no": "AB12345678"}]})
    assert got["fields"]["invoice_no"]["accepted"] and "platform" in got["fields"]["invoice_no"]["evidence"]
    # a seller UBN the history does not know, with no other source, still goes to a person
    got = check({"seller_ubn": "04595257"}, read, {"history_seller_ubn": ["12345675"]})
    assert not got["fields"]["seller_ubn"]["accepted"]
