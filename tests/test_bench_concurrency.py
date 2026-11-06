"""eval/bench_concurrency.py without a GPU: a fake reader in real spawned worker processes (tests/fake_reader.py),
plus the xpu-smi parsing and the energy arithmetic."""
import json
import sys
import textwrap
import time
from pathlib import Path

import pytest
from PIL import Image

from eval import bench_concurrency as C

FACTORY = "tests.fake_reader:FakeReader"
NAMES = [f"d{i}.jpg" for i in range(6)]


def _docs(tmp: Path) -> list[dict]:
    out = []
    for i, name in enumerate(NAMES):
        p = tmp / "img" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (i * 30, 0, 0)).save(p)
        out.append({"doc_id": f"C1/2026-09/{name}", "client_id": "C1", "month": "2026-09", "path": p})
    return out


def _cfg(tmp: Path, **kw) -> str:
    (tmp / "markers").mkdir(exist_ok=True)
    p = tmp / "fake.json"
    p.write_text(json.dumps({"sleep_s": 0.05, "marker_dir": str(tmp / "markers"), **kw}), encoding="utf-8")
    return str(p)


def _sup(tmp, cfg, k=2):
    return C.Supervisor(FACTORY, cfg, ["CPU"] * k, str(tmp / "out"), doc_timeout_s=60, load_timeout_s=120,
                        log=lambda *a: None)


def test_queue_drains_over_all_workers(tmp_path):
    docs = _docs(tmp_path)
    sup = _sup(tmp_path, _cfg(tmp_path, sleep_s=0.3), k=2)
    try:
        assert all(w["state"] == "idle" for w in sup.start())
        res = sup.run(docs)
    finally:
        sup.close()
    assert set(res["reads"]) == {d["doc_id"] for d in docs}
    assert res["failed"] == {} and res["crashes"] == [] and res["restarts"] == []
    assert sum(res["docs_per_worker"].values()) == 6 and all(v > 0 for v in res["docs_per_worker"].values())
    assert len(res["latency_s"]) == 6


def test_crash_restarts_worker_and_retries_the_document(tmp_path):
    docs = _docs(tmp_path)
    # one worker, so the retried document has to wait for the restarted process
    sup = _sup(tmp_path, _cfg(tmp_path, crash_once=["d2.jpg"], error_always=["d4.jpg"]), k=1)
    try:
        sup.start()
        res = sup.run(docs)
    finally:
        sup.close()
    crash_doc = "C1/2026-09/d2.jpg"
    assert crash_doc in res["reads"]                         # retried after the restart, read fine
    assert len(res["crashes"]) == 1 and res["crashes"][0]["doc_id"] == crash_doc
    assert res["crashes"][0]["exitcode"] == 134
    assert len(res["restarts"]) == 1 and res["restarts"][0]["load_s"] is not None
    assert [r["doc_id"] for r in res["retries"] if r["reason"] == "reader_crash"] == [crash_doc]
    # a Python error twice: not read, sent to a person, no restart
    assert res["failed"] == {"C1/2026-09/d4.jpg": {"reason": "reader_error", "attempts": 2,
                                                    "detail": res["failed"]["C1/2026-09/d4.jpg"]["detail"]}}
    assert len(res["reads"]) == 5


def _inbox(data: Path, set_name: str) -> None:
    base = data / set_name
    (base / "clients" / "C1" / "2026-09").mkdir(parents=True)
    (base / "clients.json").write_text(json.dumps({"clients": [
        {"id": "C1", "name": "Test Co", "ubn": "12345675", "period": ["2026-09-01", "2026-10-31"],
         "policy": {"meal": 1500, "taxi": 800, "lodging": 3600, "any": 30000}}]}), encoding="utf-8")
    for i, name in enumerate(NAMES):
        Image.new("RGB", (8, 8), (i * 30, 0, 0)).save(base / "clients" / "C1" / "2026-09" / name)


def test_double_crash_goes_to_a_person_as_read_review(tmp_path, monkeypatch):
    from ledger_room import pipeline
    monkeypatch.setattr(pipeline, "DATA", tmp_path / "data")
    monkeypatch.setattr(pipeline, "OUT", tmp_path / "data" / "out")
    _inbox(tmp_path / "data", "fakeset")
    cfg = _cfg(tmp_path, crash_always=["d1.jpg"])
    out = tmp_path / "res.json"
    rc = C.main(["--set", "fakeset", "--k", "1,2", "--devices", "CPU", "--factory", FACTORY, "--model", cfg,
                 "--no-inventory", "--no-power", "--settle-s", "0", "--idle-s", "0", "--out", str(out)])
    assert rc == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    bad = "C1/2026-09/d1.jpg"
    for row in res["rows"]:
        assert "error" not in row, row
        assert row["docs"] == 6 and row["docs_read"] == 5 and row["docs_not_read"] == 1
        assert row["crashes"] == 2 and [c["doc_id"] for c in row["crash_log"]] == [bad, bad]
        assert row["not_read"][bad]["reason"] == "reader_crash"
        assert row["scoring"]["read_review_not_read"] == [bad]
        assert row["power"]["wh_per_1000_docs"] is None
    assert res["rows"][1]["same_scoring_as_k1"] is True and res["rows"][1]["docs_fields_differ_vs_k1"] == 0
    # the run itself: READ_REVIEW with reason reader_crash, and in the audit log
    run_dir = tmp_path / "data" / "out" / "bench_concurrency" / "runs" / res["rows"][0]["scoring"]["run_id"]
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    rr = [e for e in result["exceptions"] if e["doc_id"] == bad]
    assert [(e["rule_id"], e["severity"], e.get("reason")) for e in rr] == [("READ_REVIEW", "review", "reader_crash")]
    log = (run_dir / "audit_log.jsonl").read_text(encoding="utf-8")
    assert '"read_failed"' in log and "reader_crash" in log


# ---- xpu-smi parsing ---------------------------------------------------------------------------------------------
DUMP = textwrap.dedent("""\
    Timestamp, DeviceId, GPU Power (W), GPU Memory Used (MiB)
    06:14:46.000,    0, 41.52, 8650.00
    06:14:47.000,    0, 180.25, 17300.00
    06:14:48.000,    0, N/A, 17300.00
    """)

STATS_JSON = json.dumps({"device_id": 0, "device_level": [
    {"metrics_type": "XPUM_STATS_GPU_UTILIZATION", "value": 97},
    {"metrics_type": "XPUM_STATS_POWER", "value": 172.4},
    {"metrics_type": "XPUM_STATS_MEMORY_USED", "value": 24576}]})

STATS_TABLE = textwrap.dedent("""\
    +-----------------------------+--------------------------------------------------------------------+
    | Device ID                   | 1                                                                  |
    +-----------------------------+--------------------------------------------------------------------+
    | GPU Utilization (%)         | 95                                                                 |
    | GPU Power (W)               | 165                                                                |
    | GPU Frequency (MHz)         | 2400                                                               |
    | GPU Memory Used (MiB)       | 16384                                                              |
    +-----------------------------+--------------------------------------------------------------------+
    """)


def test_parse_xpu_smi_dump():
    rows = C.parse_dump(DUMP)
    assert [r["power_w"] for r in rows] == [41.52, 180.25, None]
    assert rows[1]["mem_gb"] == pytest.approx(17300 / 1024, abs=1e-3)
    # columns found by name, not position
    rows2 = C.parse_dump("Timestamp, DeviceId, GPU Memory Used (MiB), GPU Utilization (%), GPU Power (W)\n"
                         "06:14:46.000, 1, 2048, 99, 150.5\n")
    assert rows2 == [{"power_w": 150.5, "mem_gb": 2.0}]
    # power column missing -> None, not a wrong number
    assert C.parse_dump("Timestamp, DeviceId, GPU Utilization (%)\n06:14:46.000, 0, 99\n") == \
        [{"power_w": None, "mem_gb": None}]


def test_parse_xpu_smi_stats_json_and_table():
    assert C.parse_stats(STATS_JSON) == {"power_w": 172.4, "mem_gb": 24.0}
    assert C.parse_stats(STATS_TABLE) == {"power_w": 165.0, "mem_gb": 16.0}
    assert C.parse_stats("xpu-smi: device not found") == {"power_w": None, "mem_gb": None}


def test_energy_per_thousand_documents():
    # 200 W for 360 s = 20 Wh; over 100 documents = 0.2 Wh each = 200 Wh per 1,000
    assert C.energy(200.0, 360.0, 100) == {"energy_wh": 20.0, "wh_per_1000_docs": 200.0}
    assert C.energy(None, 360.0, 100) == {"energy_wh": None, "wh_per_1000_docs": None}
    assert C.power_summary([(0, 100.0, 1.0), (1, 200.0, 2.0), (2, None, 3.0)]) == \
        {"mean_w": 150.0, "max_w": 200.0, "samples": 2}
    assert C.mem_peak_gb([(0, 1, 2.5), (1, 1, None), (2, 1, 7.25)]) == 7.25
    assert C.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20], 0.95) == 19


def test_smi_ids_default_and_override():
    assert C.parse_smi_ids(None, ["GPU.0", "GPU.1"]) == {"GPU.0": "0", "GPU.1": "1"}
    assert C.parse_smi_ids("GPU.0=1,GPU.1=0", ["GPU.0", "GPU.1"]) == {"GPU.0": "1", "GPU.1": "0"}
    assert C.parse_smi_ids(None, ["CPU"]) == {}


def test_sampler_streams_a_fake_xpu_smi(tmp_path):
    script = tmp_path / "fake_xpu_smi.py"
    script.write_text(textwrap.dedent("""\
        import sys, time
        print("Timestamp, DeviceId, GPU Power (W), GPU Memory Used (MiB)", flush=True)
        for i in range(50):
            print(f"06:14:{i:02d}.000, {sys.argv[3]}, {100 + i}.0, 4096", flush=True)
            time.sleep(0.05)
        """), encoding="utf-8")
    s = C.XpuSampler({"GPU.0": "0"}, cmd=[sys.executable, str(script)]).start()
    t0 = time.time()
    deadline = t0 + 15
    while time.time() < deadline and len(s.samples["GPU.0"]) < 5:
        time.sleep(0.1)
    s.stop()
    got = s.window(t0 - 1, time.time())["GPU.0"]
    assert len(got) >= 5
    assert C.power_summary(got)["mean_w"] >= 100 and C.mem_peak_gb(got) == 4.0
    assert s.mode["GPU.0"].startswith("dump:")
    assert s.raw_head["GPU.0"][0].startswith("Timestamp")
