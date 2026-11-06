"""Tiered two-card reading (ledger_room/tiered.py) with fake readers: no model, no GPU."""
import json
import threading

import pytest
from PIL import Image

from ledger_room import pipeline, tiered


# ---- helpers ------------------------------------------------------------------------------------------------------
def reading(**over) -> dict:
    """A reading as Reader.read returns it: a clean receipt, everything confirmed on the page."""
    r = {
        "fields": {"doc_type": "receipt", "invoice_no": None, "date": "2026-10-12", "invoice_period": None,
                   "seller_name": "甲店", "seller_ubn": "85927754", "buyer_name": None, "buyer_ubn": None,
                   "items": [{"desc": "影印紙", "qty": 1, "amount": 500, "source": "model"}],
                   "net": None, "tax": None, "total": 500, "note": None, "category": "office"},
        "provenance": {"doc_type": "ocr-keyword", "invoice_no": "missing", "seller_ubn": "ocr-confirmed",
                       "buyer_ubn": "no-buyer-label", "date": "ocr-confirmed", "items": "model",
                       "total": "ocr-label+model", "net": "not-printed", "tax": "not-printed", "seller_name": "model"},
        "boxes": {"total": [1, 2, 3, 4]}, "extra": {}, "ocr_lines": [{"id": "L1", "text": "x", "box": [0, 0, 1, 1],
                                                                     "conf": 0.9}],
        "raw": "{}", "parsed": True, "timing": {"vlm_s": 1.0, "ocr_s": 0.5, "total_s": 1.5},
        "model": "first", "device": "GPU.0", "image_size": [10, 10],
    }
    for k, v in over.items():
        if k in ("fields", "provenance"):
            r[k] = {**r[k], **v}
        else:
            r[k] = v
    return r


class FakeReader:
    """Returns a preset reading per file name. on_generate(name) runs inside generate (the model call)."""

    def __init__(self, model_id, device, by_name, on_generate=None, fail=()):
        self.model_id, self.device, self.by_name = model_id, device, by_name
        self.on_generate = on_generate
        self.fail = set(fail)
        self.seen: list[str] = []
        self.thinking = "off"

    def generate(self, prompt, image, max_new_tokens=360):
        return "{}", {"vlm_s": 0.01, "ttft_s": 0.0, "gen_tokens": 1}

    def read_raw(self, path):
        return {"ocr_lines": reading()["ocr_lines"], "raw": "{}", "timing": {"vlm_s": 0.01, "ocr_s": 0.0},
                "image_size": [10, 10], "model": self.model_id, "device": self.device}

    def read(self, path, raw=None):
        name = path.name
        self.seen.append(name)
        if self.on_generate:
            self.on_generate(name)
        if name in self.fail:
            raise RuntimeError("out of GPU memory")
        r = json.loads(json.dumps(self.by_name.get(name, reading())))
        r["model"], r["device"] = self.model_id, self.device
        return r


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    """data/<set>/clients/A/2026-10/{a..e}.jpg in a temp folder; pipeline outputs and the audit key go there too."""
    monkeypatch.setattr(pipeline, "DATA", tmp_path / "data")
    monkeypatch.setattr(pipeline, "OUT", tmp_path / "data" / "out")
    monkeypatch.setenv("LEDGER_AUDIT_KEY_FILE", str(tmp_path / "audit.key"))
    base = tmp_path / "data" / "t"
    folder = base / "clients" / "A" / "2026-10"
    folder.mkdir(parents=True)
    (base / "clients.json").write_text(json.dumps({"clients": [
        {"id": "A", "name": "甲公司", "ubn": "00501503", "period": ["2026-09-01", "2026-10-31"], "policy": {}}]}),
        encoding="utf-8")
    for i, n in enumerate("abcde"):
        Image.new("RGB", (40, 30), (i * 40, 200, 200)).save(folder / f"{n}.jpg")
    _, docs = pipeline.load_inbox("t")
    return docs


# ---- the fixed definition of "unsure" -------------------------------------------------------------------------------
def test_escalation_reasons_are_the_fixed_signals():
    assert tiered.escalation_reasons(reading()) == []
    assert tiered.escalation_reasons(reading(parsed=False)) == ["reply_unparsed"]
    assert tiered.escalation_reasons(reading(provenance={"total": "unconfirmed"})) == ["total_unconfirmed"]
    assert tiered.escalation_reasons(reading(provenance={"seller_ubn": "model-only",
                                                         "buyer_ubn": "unconfirmed"})) == \
        ["seller_ubn_unconfirmed", "buyer_ubn_unconfirmed"]
    bad = reading(fields={"net": 1000, "tax": 50, "total": 1100})
    assert tiered.escalation_reasons(bad) == ["amounts_inconsistent"]
    # model-only date or items alone do not send a document on (the rules do not route them to a person)
    assert tiered.escalation_reasons(reading(provenance={"date": "model-only", "invoice_no": "model-only"})) == []
    assert tiered.ROUTED_FIELDS == ("seller_ubn", "buyer_ubn", "total")
    assert tiered.UNSURE == frozenset({"model-only", "unconfirmed"})


# ---- merging ------------------------------------------------------------------------------------------------------
def test_second_reading_cannot_overwrite_a_confirmed_field():
    first = reading(provenance={"buyer_ubn": "unconfirmed"})
    second = reading(fields={"total": 999, "seller_ubn": "12345675"},
                     provenance={"total": "ocr-label+model", "seller_ubn": "ocr-confirmed"})
    m = tiered.merge(first, second, ["buyer_ubn_unconfirmed"])
    assert m["fields"]["total"] == 500 and m["fields"]["seller_ubn"] == "85927754"
    assert m["tier"]["taken_from_second"] == []


def test_unconfirmed_total_takes_second_only_when_code_confirms_it():
    first = reading(fields={"total": 480}, provenance={"total": "unconfirmed"})
    second = reading(fields={"total": 500, "net": 476, "tax": 24},
                     provenance={"total": "ocr-label+model", "net": "ocr-arith", "tax": "ocr-arith"},
                     boxes={"total": [5, 6, 7, 8]})
    m = tiered.merge(first, second, ["total_unconfirmed"])
    assert (m["fields"]["total"], m["fields"]["net"], m["fields"]["tax"]) == (500, 476, 24)
    assert m["provenance"]["total"] == "ocr-label+model" and m["boxes"]["total"] == [5, 6, 7, 8]
    assert m["tier"]["taken_from_second"] == ["total", "net", "tax"]
    assert m["timing"]["total_s"] == 3.0 and m["timing"]["first_s"] == 1.5

    still = tiered.merge(first, reading(fields={"total": 500}, provenance={"total": "unconfirmed"}),
                         ["total_unconfirmed"])
    assert still["fields"]["total"] == 480 and still["provenance"]["total"] == "unconfirmed"
    # still unconfirmed after both readings: the rules still send it to a person
    d = {"doc_id": "x", "client_id": "A", "month": "2026-10"}
    rd = pipeline.to_rule_doc(d, still["fields"], still["provenance"], still["ocr_lines"])
    assert rd.uncertain == {"total": "unconfirmed"}


def test_ubn_model_only_is_replaced_by_a_snapped_second_reading_not_by_model_only():
    first = reading(fields={"seller_ubn": "85927750"}, provenance={"seller_ubn": "model-only"})
    m = tiered.merge(first, reading(fields={"seller_ubn": "85927754"}, provenance={"seller_ubn": "snapped"}),
                     ["seller_ubn_unconfirmed"])
    assert m["fields"]["seller_ubn"] == "85927754" and m["provenance"]["seller_ubn"] == "snapped"
    m = tiered.merge(first, reading(fields={"seller_ubn": "85927751"}, provenance={"seller_ubn": "model-only"}),
                     ["seller_ubn_unconfirmed"])
    assert m["fields"]["seller_ubn"] == "85927750" and m["provenance"]["seller_ubn"] == "model-only"


def test_net_tax_from_second_only_when_the_arithmetic_holds():
    first = reading(fields={"net": 1000, "tax": 50, "total": 1100},
                    provenance={"net": "model-only", "tax": "model-only"})
    ok = reading(fields={"net": 1048, "tax": 52, "total": 1100}, provenance={"net": "model-only", "tax": "model-only"})
    m = tiered.merge(first, ok, ["amounts_inconsistent"])
    assert (m["fields"]["net"], m["fields"]["tax"]) == (1048, 52)
    wrong = reading(fields={"net": 900, "tax": 50, "total": 1100})
    m = tiered.merge(first, wrong, ["amounts_inconsistent"])
    assert (m["fields"]["net"], m["fields"]["tax"]) == (1000, 50)
    other_total = reading(fields={"net": 1143, "tax": 57, "total": 1200})
    m = tiered.merge(first, other_total, ["amounts_inconsistent"])
    assert (m["fields"]["net"], m["fields"]["tax"], m["fields"]["total"]) == (1000, 50, 1100)


def test_unparsed_first_reply_is_filled_from_second_with_its_own_provenance():
    first = reading(parsed=False, fields={"items": [], "category": None, "seller_name": None, "date": None,
                                          "seller_ubn": None},
                    provenance={"date": "missing", "seller_ubn": "missing", "items": "missing"})
    second = reading(fields={"date": "2026-10-12", "seller_ubn": "85927750"},
                     provenance={"date": "model-only", "seller_ubn": "model-only"})
    m = tiered.merge(first, second, ["reply_unparsed"])
    assert m["fields"]["date"] == "2026-10-12" and m["provenance"]["date"] == "model-only"
    assert m["fields"]["seller_ubn"] == "85927750" and m["provenance"]["seller_ubn"] == "model-only"
    assert m["fields"]["items"] and m["fields"]["category"] == "office" and m["fields"]["seller_name"] == "甲店"
    assert m["parsed"] is False and m["tier"]["second_parsed"] is True


# ---- two readers at the same time ---------------------------------------------------------------------------------
def test_only_unsure_documents_go_to_the_second_reader_and_both_cards_overlap(inbox):
    unsure = {"a.jpg": reading(provenance={"total": "unconfirmed"}),
              "c.jpg": reading(parsed=False)}
    last_started = threading.Event()
    second_waited = {}

    def first_hook(name):
        if name == "e.jpg":
            last_started.set()

    def second_hook(name):
        # the second reader is still busy with a.jpg while the first one reaches the last document
        if name == "a.jpg":
            second_waited["a"] = last_started.wait(timeout=10)

    first = FakeReader("first-9b", "GPU.0", unsure, on_generate=first_hook)
    second = FakeReader("second-27b", "GPU.1", {"a.jpg": reading(fields={"total": 500},
                                                                provenance={"total": "ocr-label+model"})},
                        on_generate=second_hook)
    tr = tiered.TieredReader(first, second)
    reads = tr.read_docs(inbox, cache=False)
    assert list(reads) == [d["doc_id"] for d in inbox]                   # original order kept
    assert sorted(second.seen) == ["a.jpg", "c.jpg"]
    assert second_waited == {"a": True}                                  # ran concurrently, not after
    assert tr.stats["escalated"] == 2 and tr.stats["reasons"] == {"total_unconfirmed": 1, "reply_unparsed": 1}
    a = reads["A/2026-10/a.jpg"]
    assert a["provenance"]["total"] == "ocr-label+model" and a["tier"]["taken_from_second"] == ["total", "net", "tax"]
    assert reads["A/2026-10/b.jpg"]["tier"] == {"escalated": False, "reasons": []}
    assert tr.model_id == "first-9b+second-27b" and tr.device == "GPU.0+GPU.1"


def test_second_reader_error_keeps_first_reading(inbox):
    first = FakeReader("f", "GPU.0", {"b.jpg": reading(provenance={"total": "unconfirmed"})})
    second = FakeReader("s", "GPU.1", {}, fail={"b.jpg"})
    tr = tiered.TieredReader(first, second)
    reads = tr.read_docs(inbox, cache=False)
    b = reads["A/2026-10/b.jpg"]
    assert b["provenance"]["total"] == "unconfirmed" and "out of GPU memory" in b["tier"]["second_error"]
    assert len(tr.stats["second_errors"]) == 1


def test_no_second_reader_counts_unsure_documents_only(inbox):
    first = FakeReader("f", "GPU.0", {"d.jpg": reading(provenance={"buyer_ubn": "model-only"})})
    tr = tiered.TieredReader(first, None, second_note="27B not on disk; skipped")
    reads = tr.read_docs(inbox, cache=False)
    assert tr.stats["escalated"] == 0 and tr.stats["unsure_docs"] == 1
    assert reads["A/2026-10/d.jpg"]["tier"]["second"] == "27B not on disk; skipped"


def test_audit_with_tiered_reader_clears_the_review_the_second_reading_settles(inbox):
    first = FakeReader("f", "GPU.0", {"a.jpg": reading(fields={"total": 480}, provenance={"total": "unconfirmed"}),
                                      "b.jpg": reading(fields={"total": 480}, provenance={"total": "unconfirmed"})})
    second = FakeReader("s", "GPU.1", {"a.jpg": reading(fields={"total": 500}, provenance={"total": "ocr-label+model"}),
                                       "b.jpg": reading(fields={"total": 490}, provenance={"total": "unconfirmed"})})
    tr = tiered.TieredReader(first, second)
    run = pipeline.audit("t", tr, run_id="tiered-test", cache=False, read_fn=tr.read_docs)
    review = {e["doc_id"] for e in run["exceptions"] if e["rule_id"] == "READ_REVIEW"}
    assert review == {"A/2026-10/b.jpg"}            # a.jpg settled by the second reading; b.jpg still goes to a person
    docs = {d["doc_id"]: d for d in run["docs"]}
    assert docs["A/2026-10/a.jpg"]["tier"]["taken_from_second"] == ["total", "net", "tax"]
    log = (pipeline.OUT / run["audit_log"]).read_text(encoding="utf-8").splitlines()
    start = json.loads(log[0])
    assert start["model"] == "f+s" and start["device"] == "GPU.0+GPU.1"
    assert pipeline.AuditLog.verify(pipeline.OUT / run["audit_log"])[0]


def test_audit_without_read_fn_is_unchanged(inbox):
    first = FakeReader("f", "GPU.0", {})
    run = pipeline.audit("t", first, run_id="plain-test", cache=False)
    assert all("tier" not in d for d in run["docs"])


# ---- bench script -------------------------------------------------------------------------------------------------
def test_bench_refuses_escalate_all_outside_dry_run():
    from eval import bench_tiered
    with pytest.raises(SystemExit):
        bench_tiered.main(["--escalate-all", "--set", "heldout"])


def test_bench_private_set_writes_no_document_ids(inbox):
    from eval import bench_tiered
    first = FakeReader("f", "GPU.0", {"c.jpg": reading(provenance={"total": "unconfirmed"})})
    second = FakeReader("s", "GPU.1", {"c.jpg": reading(provenance={"total": "unconfirmed"})})
    out = bench_tiered.run_mode(tiered.TieredReader(first, second), "t", None, True, "private test")
    txt = json.dumps(out, ensure_ascii=False)
    assert "c.jpg" not in txt and "A/2026-10" not in txt
    assert out["docs"] == 5 and out["sent_to_second"] == 1 and out["key_fields_wrong"] is None
    assert out["docs_for_person"] == 1


def test_bench_expected_for_new_sets(tmp_path, monkeypatch):
    from eval import bench_tiered
    monkeypatch.setattr(bench_tiered, "ROOT", tmp_path)
    (tmp_path / "eval").mkdir()
    assert bench_tiered.expected_for("hard", {"x"}) is None
    (tmp_path / "eval" / "expected_hard.json").write_text(json.dumps({"docs": [{"doc_id": "x"}, {"doc_id": "y"}]}),
                                                          encoding="utf-8")
    assert [d["doc_id"] for d in bench_tiered.expected_for("hard", {"x"})["docs"]] == ["x"]


# ---- second reader in its own process -----------------------------------------------------------------------------
class ProcFakeReader:
    """Loaded inside the child process by ProcessReader(factory="tests.test_tiered:make_proc_fake").
    model_dir "boom" ends the process on b.jpg, like a native abort in the GPU plugin."""

    def __init__(self, model_dir, device):
        self.model_dir, self.model_id, self.device, self.thinking = model_dir, f"proc-{model_dir}", device, "off"

    def generate(self, prompt, image, max_new_tokens=360):
        return "{}", {"vlm_s": 0.01, "ttft_s": 0.0, "gen_tokens": 1}

    def read(self, path, raw=None):
        import os
        if self.model_dir == "boom" and path.name == "b.jpg":
            os._exit(134)
        r = reading(fields={"total": 500}, provenance={"total": "ocr-label+model"})
        r["model"], r["device"] = self.model_id, self.device
        return r


def make_proc_fake(model_dir, device):
    return ProcFakeReader(model_dir, device)


def test_second_reader_in_its_own_process(inbox):
    first = FakeReader("f", "GPU.0", {"a.jpg": reading(fields={"total": 480}, provenance={"total": "unconfirmed"})})
    second = tiered.ProcessReader("ok", "GPU.1", factory="tests.test_tiered:make_proc_fake", poll_s=0.2)
    try:
        import os
        assert second.pid != os.getpid() and second.model_id == "proc-ok" and second.device == "GPU.1"
        tr = tiered.TieredReader(first, second)
        reads = tr.read_docs(inbox, cache=False)
        a = reads["A/2026-10/a.jpg"]
        assert a["fields"]["total"] == 500 and a["tier"]["taken_from_second"] == ["total", "net", "tax"]
        assert tr.stats["second_reader_crashed"] == 0
    finally:
        second.close()
    assert not second.proc.is_alive()


def test_second_process_crash_holds_the_document_and_the_run_goes_on(inbox):
    first = FakeReader("f", "GPU.0", {"a.jpg": reading(fields={"total": 480}, provenance={"total": "unconfirmed"}),
                                      "b.jpg": reading(parsed=False),
                                      "d.jpg": reading(provenance={"seller_ubn": "model-only"})})
    second = tiered.ProcessReader("boom", "GPU.1", factory="tests.test_tiered:make_proc_fake", poll_s=0.2)
    try:
        tr = tiered.TieredReader(first, second)
        run = pipeline.audit("t", tr, run_id="crash-test", cache=False, read_fn=tr.read_docs)
    finally:
        second.close()
    st = tr.stats
    assert st["second_reader_crashed"] == 2 and st["second_reader_crashed_docs"] == ["A/2026-10/b.jpg",
                                                                                    "A/2026-10/d.jpg"]
    assert "exited with code 134" in st["second_reader_crash"]
    docs = {d["doc_id"]: d for d in run["docs"]}
    assert docs["A/2026-10/a.jpg"]["fields"]["total"] == 500            # read before the crash: kept
    assert "second_reader_crashed" in docs["A/2026-10/b.jpg"]["tier"]
    review = {e["doc_id"] for e in run["exceptions"] if e["rule_id"] == "READ_REVIEW"}
    assert {"A/2026-10/b.jpg", "A/2026-10/d.jpg"} <= review           # b.jpg had no review of its own
    assert "A/2026-10/a.jpg" not in review


# ---- blind sets run last and only after clean runs ------------------------------------------------------------------
def test_blind_sets_run_last_and_only_after_clean_runs():
    from eval import bench_tiered as bt
    assert bt.order_sets(["blind3", "heldout", "hard", "heldout"]) == ["heldout", "hard", "blind3"]
    assert bt.blind_gate("blind3", {"heldout": True, "hard": True}, {}, "abc", False) is None
    assert "hard" in bt.blind_gate("blind3", {"heldout": True, "hard": False}, {}, "abc", False)
    assert "hard" in bt.blind_gate("blind3", {"heldout": True}, {}, "abc", False)
    earlier = {"hard": {"measured_on_b70": True, "code_rev": "abc"}}
    assert bt.blind_gate("blind3", {"heldout": True}, earlier, "abc", False) is None
    assert bt.blind_gate("blind3", {"heldout": True}, earlier, "def", False) is not None
    ran = {"blind3": {"dry_run": False, "status": "started", "generated_at": "x"}}
    assert "already" in bt.blind_gate("blind3", {"heldout": True, "hard": True}, ran, "abc", False)
    assert bt.blind_gate("blind3", {"heldout": True, "hard": True}, ran, "abc", True) is None
