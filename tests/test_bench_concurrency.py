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
    # one restart after the crash, and one after each reader error (the worker ends itself: on the B70 the GPU
    # plugin is unusable after an inference exception), so the retry runs in a fresh process
    assert [r["reason"] for r in res["restarts"]] == [f"crash on {crash_doc}", "after reader_error",
                                                       "after reader_error"]
    assert all(r["load_s"] is not None for r in res["restarts"])
    assert [r["doc_id"] for r in res["retries"] if r["reason"] == "reader_crash"] == [crash_doc]
    # a Python error twice: not read, sent to a person; reader errors are not counted as crashes
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


# exactly as printed on the B70 host (2026-11-07): whitespace-aligned, timestamp truncated with "..."
HOST_DUMP = textwrap.dedent("""\
    Timestamp      DeviceId    power.draw (W)    memory.used (MiB)
    18:03:53...           0             76.83         1547.6328125
    18:03:54...           0             76.10         1547.6640625
    """)

HOST_STATS = json.dumps({
    "device_index": 0, "pci_bdf": "0000:17:00.0", "device_type": "discrete",
    "timestamps": {"start": "18:10:01", "end": "18:10:02", "elapsed_seconds": 1.0},
    "power": {"gpu_power_w": {"tile_0": {"avg": 75.9, "min": 75.1, "max": 77.0, "current": 76.4}},
              "energy_consumed_j": 13.03},
    "frequency": {"gpu_frequency_mhz": {"tile_0": {"current": 2400}}}})


def test_parse_xpu_smi_dump_host_format():
    rows = C.parse_dump(HOST_DUMP)
    assert [r["power_w"] for r in rows] == [76.83, 76.10]
    assert rows[0]["mem_gb"] == pytest.approx(1547.6328125 / 1024, abs=1e-3)
    cols = C.dump_columns(HOST_DUMP.splitlines()[0])
    assert cols["names"] == ["Timestamp", "DeviceId", "power.draw (W)", "memory.used (MiB)"]


def test_parse_xpu_smi_dump_old_csv_names():
    rows = C.parse_dump(DUMP)
    assert [r["power_w"] for r in rows] == [41.52, 180.25, None]
    assert rows[1]["mem_gb"] == pytest.approx(17300 / 1024, abs=1e-3)
    # columns found by name, not position
    rows2 = C.parse_dump("Timestamp, DeviceId, GPU Memory Used (MiB), GPU Utilization (%), GPU Power (W)\n"
                         "06:14:46.000, 1, 2048, 99, 150.5\n")
    assert rows2 == [{"power_w": 150.5, "mem_gb": 2.0, "util_pct": 99.0}]
    # power column missing -> None, not a wrong number
    assert C.parse_dump("Timestamp, DeviceId, GPU Utilization (%)\n06:14:46.000, 0, 99\n") == \
        [{"power_w": None, "mem_gb": None, "util_pct": 99.0}]


def test_dump_command_uses_long_flags():
    cmd = C.XpuSampler({"GPU.0": "0"}, cmd=["xpu-smi-x"]).dump_cmd("1")
    assert cmd[:2] == ["xpu-smi-x", "dump"]
    assert all(not (c.startswith("-") and not c.startswith("--")) for c in cmd[2:])
    assert cmd[cmd.index("--device") + 1] == "1" and cmd[cmd.index("--metrics") + 1] == "0,1,18"
    assert "--interval" in cmd and "--number" in cmd


def test_parse_xpu_smi_stats_host_json_old_json_and_table():
    assert C.parse_stats(HOST_STATS) == {"power_w": 76.4, "mem_gb": None, "energy_j": 13.03, "util_pct": None}
    two_tiles = json.loads(HOST_STATS)
    two_tiles["power"]["gpu_power_w"]["tile_1"] = {"avg": 20.0, "current": 20.0}
    two_tiles["memory"] = {"memory_used_mib": 8192}
    # utilisation over two tiles is their mean, not their sum
    two_tiles["utilization"] = {"gpu_utilization_pct": {"tile_0": {"current": 90.0}, "tile_1": {"current": 70.0}}}
    assert C.parse_stats(json.dumps(two_tiles)) == {"power_w": 96.4, "mem_gb": 8.0, "energy_j": 13.03,
                                                    "util_pct": 80.0}
    assert C.parse_stats(STATS_JSON) == {"power_w": 172.4, "mem_gb": 24.0, "energy_j": None, "util_pct": 97.0}
    assert C.parse_stats(STATS_TABLE) == {"power_w": 165.0, "mem_gb": 16.0, "energy_j": None, "util_pct": 95.0}
    assert C.parse_stats("xpu-smi: device not found") == {"power_w": None, "mem_gb": None, "energy_j": None,
                                                          "util_pct": None}


def test_energy_per_thousand_documents():
    # 200 W for 360 s = 20 Wh; over 100 documents = 0.2 Wh each = 200 Wh per 1,000
    assert C.energy(200.0, 360.0, 100) == {"energy_wh": 20.0, "wh_per_1000_docs": 200.0}
    assert C.energy(None, 360.0, 100) == {"energy_wh": None, "wh_per_1000_docs": None}
    assert C.power_summary([(0, 100.0, 1.0), (1, 200.0, 2.0), (2, None, 3.0)]) == \
        {"mean_w": 150.0, "max_w": 200.0, "samples": 2}
    assert C.mem_peak_gb([(0, 1, 2.5), (1, 1, None), (2, 1, 7.25)]) == 7.25
    assert C.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20], 0.95) == 19


def test_energy_counter_preferred_then_fallback():
    by_dev = {"GPU.0": {"mean_w": 200.0}}
    # counter: 72,000 J over 360 s (200 W) = 20 Wh; 100 docs -> 200 Wh per 1,000
    e = C.combine_energy({"GPU.0": {"energy_j": 1000.0, "t": 0.0}}, {"GPU.0": {"energy_j": 73000.0, "t": 360.0}},
                         by_dev, 200.0, 360.0, 100)
    assert e["energy_source"].startswith("xpu-smi energy counter")
    assert (e["energy_wh"], e["wh_per_1000_docs"]) == (20.0, 200.0)
    assert (e["sampled_energy_wh"], e["sampled_wh_per_1000_docs"]) == (20.0, 200.0)
    # counter went backwards (reset) -> sampled mean W
    e = C.combine_energy({"GPU.0": {"energy_j": 5000.0, "t": 0.0}}, {"GPU.0": {"energy_j": 13.0, "t": 360.0}},
                         by_dev, 200.0, 360.0, 100)
    assert e["energy_source"].startswith("sampled") and e["wh_per_1000_docs"] == 200.0
    assert "did not increase" in e["energy_counter"]["GPU.0"]["status"]
    # a per-window value, not a counter (13 J -> 14 J over 360 s = 0.003 W vs 200 W sampled) -> sampled
    e = C.combine_energy({"GPU.0": {"energy_j": 13.0, "t": 0.0}}, {"GPU.0": {"energy_j": 14.0, "t": 360.0}},
                         by_dev, 200.0, 360.0, 100)
    assert e["energy_source"].startswith("sampled")
    # no counter, no samples -> nothing invented
    e = C.combine_energy({}, {}, {"GPU.0": {"mean_w": None}}, None, 360.0, 100)
    assert e["energy_source"] is None and e["wh_per_1000_docs"] is None


def test_smi_ids_default_and_override():
    assert C.parse_smi_ids(None, ["GPU.0", "GPU.1"]) == {"GPU.0": "0", "GPU.1": "1"}
    assert C.parse_smi_ids("GPU.0=1,GPU.1=0", ["GPU.0", "GPU.1"]) == {"GPU.0": "1", "GPU.1": "0"}
    assert C.parse_smi_ids(None, ["CPU"]) == {}


def test_sampler_streams_a_fake_xpu_smi(tmp_path):
    script = tmp_path / "fake_xpu_smi.py"
    script.write_text(textwrap.dedent("""\
        import sys, time
        args = sys.argv[1:]
        if any(a in ("-d", "-m", "-i", "-n") for a in args):   # like the host build
            print("[Error] The following arguments were not expected: " + " ".join(args), flush=True)
            sys.exit(1)
        dev = args[args.index("--device") + 1]
        print("Timestamp      DeviceId    power.draw (W)    memory.used (MiB)", flush=True)
        for i in range(50):
            print(f"18:03:{i:02d}...           {dev}             {100 + i}.00         4096.0", flush=True)
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


# ---- GPU utilisation ---------------------------------------------------------------------------------------------
UTIL_HEADER_CSV = "Timestamp, DeviceId, GPU Utilization (%), power.draw (W), memory.used (MiB)"
UTIL_HEADER_WS = "Timestamp      DeviceId    GPU Utilization (%)    power.draw (W)    memory.used (MiB)"


@pytest.mark.parametrize("header,row", [
    (UTIL_HEADER_CSV, "06:14:46.000,    0, 97.5, 180.25, 2048.0"),
    (UTIL_HEADER_WS, "18:03:53...           0           97.5            180.25         2048.0"),
])
def test_utilisation_column_comma_and_whitespace(header, row):
    cols = C.dump_columns(header)
    assert cols["names"] == ["Timestamp", "DeviceId", "GPU Utilization (%)", "power.draw (W)", "memory.used (MiB)"]
    assert cols["names"][cols["util"]] == "GPU Utilization (%)" and cols["raw"] == header
    assert C.parse_dump_row(cols, row) == {"power_w": 180.25, "mem_gb": 2.0, "util_pct": 97.5}
    assert C.parse_dump_row(cols, row.replace("97.5", "N/A"))["util_pct"] is None


def test_utilisation_column_names():
    # the dotted name a newer build probably prints; memory utilisation is not GPU utilisation
    cols = C.dump_columns("Timestamp, DeviceId, GPU Memory Utilization (%), utilization.gpu (%), power.draw (W)")
    assert cols["names"][cols["util"]] == "utilization.gpu (%)"
    assert C.parse_dump(f"{cols['raw']}\n06:14:46.000, 0, 12, 88, 150\n")[0]["util_pct"] == 88.0
    assert not C.is_gpu_util("GPU Memory Utilization (%)") and C.is_gpu_util("GPU Utilization (%)")


def test_utilisation_missing_is_null_with_raw_header():
    header = "Timestamp      DeviceId    power.draw (W)    memory.used (MiB)"
    cols = C.dump_columns(header)
    assert "util" not in cols
    assert C.parse_dump_row(cols, "18:03:53...  0  76.83  1547.6")["util_pct"] is None
    s = C.XpuSampler({"GPU.0": "0"})
    s.header["GPU.0"] = cols["raw"]
    out = C.util_block(s, "GPU.0", C.util_summary([(0, 76.8, 1.5, None)]))
    assert out["mean_pct"] is None and out["p95_pct"] is None and out["frac_ge_90"] is None
    assert out["column"] is None and out["raw_header"] == header and "util" in out["note"]


def test_utilisation_summary():
    vals = [10, 50, 91, 95, 99, 100, 100, 100, 90, 0]
    smp = [(i, 100.0, 1.0, u) for i, u in enumerate(vals)] + [(10, 1.0, 1.0, None)]
    u = C.util_summary(smp)
    assert u["samples"] == 10 and u["mean_pct"] == 73.5
    assert u["p50_pct"] == 91 and u["p95_pct"] == 100
    assert u["frac_ge_90"] == 0.7   # 91, 95, 99, 100, 100, 100, 90
    assert C.util_summary([]) == {"mean_pct": None, "p50_pct": None, "p95_pct": None, "frac_ge_90": None,
                                  "samples": 0}


def test_sampler_memory_is_bounded_and_samples_spill_to_a_file(tmp_path):
    s = C.XpuSampler({"GPU.0": "0"}, max_keep=50, spill_dir=tmp_path)
    s.open_spill()
    for i in range(5000):
        s._add("GPU.0", 100.0 + (i % 2) * 100, 4.0, 95.0 if i % 4 else 40.0)
    s.close_spill()
    assert len(s.samples["GPU.0"]) <= 50 and s.stride["GPU.0"] > 1
    snap = s.acc_snapshot()["GPU.0"]   # exact over all 5,000, not over the thinned list
    assert snap["power"] == {"mean_w": 150.0, "max_w": 200.0, "samples": 5000}
    assert snap["util"]["samples"] == 5000 and snap["util"]["frac_ge_90"] == 0.75
    lines = Path(s.spill_files["GPU.0"]).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "unix_time,power_w,mem_gb,util_pct" and len(lines) == 5001
    s.reset_acc()
    assert s.acc_snapshot()["GPU.0"]["power"]["samples"] == 0


def test_restarts_per_hour():
    # 3 restarts in 30 minutes = 6 an hour; 1 crash in an 85 s run = 42.35 an hour (why a short run says little)
    assert C.per_hour(3, 1800) == 6.0
    assert C.per_hour(1, 85) == 42.35
    assert C.per_hour(0, 3600) == 0.0
    assert C.per_hour(2, 0) is None


def test_results_file_is_trimmed_under_the_limit():
    passes = [{"pass": i, "set": "s", "docs": 50, "util_mean_pct": {"GPU.0": 90.0}, "power_mean_w": {"GPU.0": 200.0},
               "latency_s": {"p50": 3.0, "p95": 5.0}, "pad": "x" * 200} for i in range(2000)]
    doc = {"notes": [], "rows": [{"k": 2, "docs_read": 100000, "crash_log": [{"x": "y" * 100}] * 100,
                                  "passes": passes}]}
    txt = C.fit_json(doc, limit=60_000)
    assert len(txt.encode("utf-8")) <= 60_000
    out = json.loads(txt)
    assert out["rows"][0]["docs_read"] == 100000 and out["rows"][0]["passes_trimmed"]["dropped"] > 0
    assert any("trimmed" in n for n in out["notes"])
    small = {"notes": [], "rows": [{"k": 1}]}
    assert json.loads(C.fit_json(small)) == small


# ---- long run with the fake reader --------------------------------------------------------------------------------
FAKE_SMI = textwrap.dedent("""\
    import json, sys, time
    args = sys.argv[1:]
    if args[0] == "discovery":
        print("Device 0: fake")
        sys.exit(0)
    if args[0] == "stats":   # a running energy counter at about 150 W
        print(json.dumps({"power": {"gpu_power_w": {"tile_0": {"current": 150.0}},
                                    "energy_consumed_j": round(time.time() * 150.0, 1)}}))
        sys.exit(0)
    print("Timestamp      DeviceId    GPU Utilization (%)    power.draw (W)    memory.used (MiB)", flush=True)
    i = 0
    while True:
        print(f"18:03:53...           0           {95 if i % 5 else 60}            150.00         4096.0", flush=True)
        i += 1
        time.sleep(0.05)
    """)


def _longrun_env(tmp_path, monkeypatch, **cfg):
    from ledger_room import pipeline
    monkeypatch.setattr(pipeline, "DATA", tmp_path / "data")
    monkeypatch.setattr(pipeline, "OUT", tmp_path / "data" / "out")
    _inbox(tmp_path / "data", "fakeset")
    _inbox(tmp_path / "data", "fakeset2")
    return _cfg(tmp_path, **cfg)


def test_longrun_repeat_gives_one_scoring_block_per_pass_and_set(tmp_path, monkeypatch):
    from eval import bench_tiered
    # an answer key for the fake sets, so F1 and key fields are scored in every pass
    monkeypatch.setattr(bench_tiered, "expected_for", lambda name, ids: {"docs": [
        {"doc_id": d, "issues": [], "truth": {"doc_type": "receipt", "total": 100}} for d in sorted(ids)]})
    cfg = _longrun_env(tmp_path, monkeypatch, crash_once=["d2.jpg"], error_always=["d4.jpg"])
    out = tmp_path / "longrun.json"
    rc = C.main(["--sets", "fakeset,fakeset2", "--repeat", "2", "--k", "2", "--devices", "CPU", "--factory", FACTORY,
                 "--model", cfg, "--no-inventory", "--no-power", "--settle-s", "0", "--idle-s", "0",
                 "--out", str(out)])
    assert rc == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["mode"] == "longrun" and res["docs_per_pass"] == 12
    row = res["rows"][0]
    assert "error" not in row, row
    assert row["stopped_by"] == "repeat" and row["passes_complete"] == 4
    assert [(p["pass"], p["set"]) for p in row["passes"]] == [(1, "fakeset"), (1, "fakeset2"), (2, "fakeset"),
                                                              (2, "fakeset2")]
    for p in row["passes"]:
        assert not p["partial"] and p["docs"] == 6 and p["docs_read"] == 5 and p["docs_not_read"] == 1
        assert p["not_read_reasons"] == {"reader_error": 1}
        assert p["key_fields_wrong"] is not None and p["key_fields_compared"] > 0   # (F1: no issues in this key)
    for name in ("fakeset", "fakeset2"):
        st = row["stability"][name]
        assert st["complete_passes"] == 2 and st["all_complete_passes_same_scoring"] is True
        assert len(st["rule_f1_values"]) == 1 and st["max_docs_fields_differ_vs_first_pass"] == 0
    # d4 fails twice in each of the 4 blocks (8 restarts after reader errors), d2 crashes once (1 restart)
    assert row["docs"] == 24 and row["docs_read"] == 20 and row["docs_not_read_reader_failure"] == 4
    assert row["crashes"] == 1 and row["restarts"] == 9 and row["restarts_after_reader_error"] == 8
    assert row["restarts_per_hour"] == pytest.approx(9 * 3600 / row["wall_s"], rel=0.02)
    cpu = row["per_device"]["CPU"]
    assert cpu["docs_read"] == 20 and cpu["docs_not_read_reader_failure"] == 4 and cpu["restarts"] == 9
    assert cpu["crashes_per_hour"] == pytest.approx(3600 / row["wall_s"], rel=0.02)
    assert cpu["reached_1000_docs"] is False and any("< 1,000" in n for n in res["notes"])
    # the reads went to side files, one per block
    side = tmp_path / "data" / "out" / "bench_concurrency" / "longrun"
    assert len(list(side.glob("*/blocks/*.jsonl"))) == 4


def test_longrun_duration_stops_and_records_utilisation(tmp_path, monkeypatch):
    cfg = _longrun_env(tmp_path, monkeypatch, sleep_s=0.05)
    smi = tmp_path / "fake_smi.py"
    smi.write_text(FAKE_SMI, encoding="utf-8")
    out = tmp_path / "longrun.json"
    duration_min = 0.04   # 2.4 s of read wall clock
    t = time.time()
    rc = C.main(["--sets", "fakeset,fakeset2", "--duration-min", str(duration_min), "--k", "2", "--devices", "CPU",
                 "--factory", FACTORY, "--model", cfg, "--no-inventory", "--settle-s", "0", "--idle-s", "0",
                 "--xpu-smi-ids", "CPU=0", "--xpu-smi-cmd", f"{sys.executable} {smi}", "--out", str(out)])
    assert rc == 0 and time.time() - t < 120
    assert out.stat().st_size < C.MAX_JSON_BYTES
    row = json.loads(out.read_text(encoding="utf-8"))["rows"][0]
    assert "error" not in row, row
    assert row["stopped_by"] == "duration"
    assert duration_min * 60 <= row["wall_s"] < duration_min * 60 + 20   # stops once the last documents are back
    assert row["passes_started"] >= 2 and row["docs_read"] == row["docs"] > 12
    assert sum(p["docs"] for p in row["passes"]) == row["docs"]
    assert all(not p["partial"] for p in row["passes"][:-1])   # only the last block can be partial
    cpu = row["per_device"]["CPU"]
    u = cpu["utilisation"]
    assert u["column"] == "GPU Utilization (%)" and u["raw_header"].startswith("Timestamp")
    assert u["samples"] > 0 and 60 <= u["mean_pct"] <= 95 and u["p95_pct"] == 95 and 0.5 < u["frac_ge_90"] < 1
    assert cpu["power"]["mean_w"] == 150.0 and cpu["wh_per_1000_docs"] is not None
    assert cpu["xpu_smi_peak_used_gb"] == 4.0
    side = tmp_path / "data" / "out" / "bench_concurrency" / "longrun"
    csv = next(side.glob("*/xpu_smi_CPU.csv")).read_text(encoding="utf-8").splitlines()
    assert csv[0] == "unix_time,power_w,mem_gb,util_pct" and len(csv) > 1
