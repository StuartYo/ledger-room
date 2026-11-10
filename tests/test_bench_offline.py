"""Target judging in eval/bench_b70.py and address checks in eval/offline_check.py (no model needed)."""
from pathlib import Path

from eval import bench_b70 as B
from eval.offline_check import is_local

ROOT = Path(__file__).resolve().parents[1]


def test_thresholds_are_the_frozen_readme_numbers():
    t9, t27 = B.TARGETS["Qwen3.5-9B-int4-ov"], B.TARGETS["Qwen3.5-27B-int4-ov"]
    assert (B.LAPTOP_9B_FIELDS_WRONG, B.LAPTOP_9B_FIELDS_COMPARED) == (20, 337)
    assert t9["key_fields_wrong_max"] == round(20 / 337 + 0.01, 4)
    assert (t9["f1_min"], t9["sec_per_doc_pass"], t9["sec_per_doc_goal"]) == (0.95, 6.0, 3.0)
    assert (t27["f1_min"], t27["sec_per_doc_pass"], t27["key_fields_wrong_below"]) == (0.95, 12.0, "9B")
    readme = (ROOT / "docs" / "evaluation.md").read_text(encoding="utf-8")   # the frozen targets table lives here
    for s in ["no worse than the laptop 9B plus 1 point", "at least 0.95", "pass 6 s, goal 3 s", "at most 12 s",
              "6% (20/337)"]:
        assert s in readme, f"docs/evaluation.md target text changed: {s!r}; bench_b70 thresholds must follow the frozen table"


def _res(fer, f1, sec, pure=True):
    r = {"reading": {"field_error_rate": fer}, "ledger_room": {"micro": {"f1": f1}, "sec_per_doc": {"mean": sec}}}
    if pure:
        r["pure_model"] = {"micro": {"f1": 0.2}}
    return r


def _by(rows, model, target):
    return next(r for r in rows if r["model"] == model and r["target"].startswith(target))


def test_judge_9b_and_27b():
    rows = B.judge({"Qwen3.5-9B-int4-ov": _res(0.069, 0.96, 5.9),
                    "Qwen3.5-27B-int4-ov": _res(0.05, 0.94, 12.5, pure=False)}, binding=True)
    m9, m27 = "Qwen3.5-9B-int4-ov", "Qwen3.5-27B-int4-ov"
    assert _by(rows, m9, "Key fields")["pass"] is True
    assert _by(rows, m9, "Ledger Room micro F1")["pass"] is True
    assert _by(rows, m9, "Read time per document, whole")["pass"] is True
    assert _by(rows, m9, "Read time per document, goal")["pass"] is False
    assert _by(rows, m9, "Pure-model")["pass"] is True
    k27 = _by(rows, m27, "Key fields")
    assert k27["pass"] is True and "same run" in k27["threshold"]
    assert _by(rows, m27, "Ledger Room micro F1")["pass"] is False
    assert _by(rows, m27, "Read time per document, whole")["pass"] is False
    assert _by(rows, m27, "Pure-model")["pass"] is False
    assert all(r["binding"] for r in rows)


def test_judge_27b_without_9b_uses_laptop_9b_and_errors_fail():
    rows = B.judge({"Qwen3.5-27B-int4-ov": _res(0.06, 0.97, 10.0),
                    "Qwen3.5-9B-int4-ov": {"error": "RuntimeError: out of memory"}}, binding=True)
    k27 = _by(rows, "Qwen3.5-27B-int4-ov", "Key fields")
    assert k27["pass"] is False and "laptop 9B" in k27["threshold"]      # 0.06 is not below 20/337 = 0.0593
    assert _by(rows, "Qwen3.5-9B-int4-ov", "run")["pass"] is False


def test_missing_numbers_are_not_passes():
    rows = B.judge({"Qwen3.5-9B-int4-ov": _res(None, None, None)}, binding=False)
    assert [r["pass"] for r in rows if r["target"] != "Pure-model baseline with the same model"] == [None] * 4


def test_pick_gpu_prefers_intel_discrete():
    inv = {"devices": [{"id": "CPU"}, {"id": "GPU.0", "name": "Intel(R) UHD Graphics (iGPU)", "type": "Type.INTEGRATED",
                                       "memory_gb": 14.4},
                       {"id": "GPU.1", "name": "NVIDIA GeForce RTX 4050 Laptop GPU (dGPU)", "type": "Type.DISCRETE",
                        "memory_gb": 6.0}]}
    assert B.pick_gpu(inv) == "GPU.0"
    inv["devices"].append({"id": "GPU.2", "name": "Intel(R) Arc(TM) Pro B70 Graphics (dGPU)", "type": "Type.DISCRETE",
                           "memory_gb": 31.9})
    assert B.pick_gpu(inv) == "GPU.2"
    assert B.pick_gpu({"devices": [{"id": "CPU"}]}) is None


def test_is_local():
    for h in ["127.0.0.1", "127.8.9.1", "::1", "localhost", "0.0.0.0", "::ffff:127.0.0.1", None, ""]:
        assert is_local(h), h
    for h in ["64.233.189.100", "192.168.0.10", "10.0.0.5", "www.google-analytics.com", "2001:4860:4860::8888"]:
        assert not is_local(h), h
