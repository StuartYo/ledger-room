"""Stage II measurement on the Intel Arc Pro B70: one command, one results file.

  python -m eval.bench_b70                 # on the B70 host: inventory, models, 9B + 27B on the held-out set
  python -m eval.bench_b70 --dry-run       # on the laptop: 2B, Intel iGPU, 3 held-out documents
  python -m eval.bench_b70 --inventory-only

What it does, in order:
  1. Inventory: OS, CPU, RAM, Python, OpenVINO / GenAI versions, every OpenVINO device with its name, type,
     driver and memory size, free disk at the models folder.
  2. Models: makes sure the pre-converted OpenVINO INT4 builds are on disk (OpenVINO/Qwen3.5-9B-int4-ov, 5.85 GB, and
     OpenVINO/Qwen3.5-27B-int4-ov, 14.87 GB, both Apache-2.0 on Hugging Face, checked 2026-11-02); downloads what is
     missing after checking free disk. A token, if one is needed, is read from HF_TOKEN by huggingface_hub; this
     script never prints or stores it.
  3. Per model, fresh reads only (cache off): Ledger Room on the frozen held-out set, then the pure-model baseline
     with the same model; seconds per document (OCR + model + zoomed re-reads, the README definition), documents per
     hour, reading accuracy, F1, and peak memory (process RAM, and GPU memory where the OS exposes it).
  4. Judges each Stage II target against the numbers frozen in README.md on 2026-10-14 (below) and writes
     eval/results_b70.json (dry run: eval/results_b70_dryrun.json; it never overwrites the real file).

The thresholds are copied from README.md "Stage II targets on the B70" and must not be edited here.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- frozen targets (README.md, "Stage II targets on the B70", committed 2026-10-14; do not change) ----------------
LAPTOP_9B_FIELDS_WRONG = 20
LAPTOP_9B_FIELDS_COMPARED = 337          # README: "Qwen3.5-9B-int4-ov ... 6% (20/337)", laptop CPU, held-out
TARGETS = {
    "Qwen3.5-9B-int4-ov": {
        "key_fields_wrong_max": round(LAPTOP_9B_FIELDS_WRONG / LAPTOP_9B_FIELDS_COMPARED + 0.01, 4),  # laptop 9B + 1 pt
        "f1_min": 0.95,
        "sec_per_doc_pass": 6.0,
        "sec_per_doc_goal": 3.0,
    },
    "Qwen3.5-27B-int4-ov": {
        "key_fields_wrong_below": "9B",   # below the 9B (the B70 9B of the same run; laptop 9B if 9B was not run)
        "f1_min": 0.95,
        "sec_per_doc_pass": 12.0,
    },
}
MODELS = {
    "9b": ("OpenVINO/Qwen3.5-9B-int4-ov", 5.85),
    "27b": ("OpenVINO/Qwen3.5-27B-int4-ov", 14.87),
    "2b": ("OpenVINO/Qwen3.5-2B-int4-ov", 1.99),
}
SET = "heldout"


# ---- inventory --------------------------------------------------------------------------------------------------
def _try(f, default=None):
    try:
        return f()
    except Exception:
        return default


def inventory(models_root: Path) -> dict:
    import psutil
    inv = {
        "os": platform.platform(), "machine": platform.machine(), "python": sys.version.split()[0],
        "cpu": _cpu_name(), "cpu_logical": psutil.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "host": platform.node(),
    }
    try:
        import openvino as ov
        inv["openvino"] = ov.__version__
        inv["openvino_genai"] = _try(lambda: __import__("openvino_genai").__version__)
        core = ov.Core()
        devs = []
        for d in core.available_devices:
            e = {"id": d, "name": _try(lambda: core.get_property(d, "FULL_DEVICE_NAME"))}
            if d.startswith("GPU"):
                e["type"] = str(_try(lambda: core.get_property(d, "DEVICE_TYPE"), ""))
                mem = _try(lambda: core.get_property(d, "GPU_DEVICE_TOTAL_MEM_SIZE"))
                e["memory_gb"] = round(mem / 2**30, 1) if mem else None
                e["driver"] = str(_try(lambda: core.get_property(d, "GPU_DRIVER_VERSION"),
                                       _try(lambda: core.get_property(d, "DRIVER_VERSION"), "")) or "")
                e["uarch"] = str(_try(lambda: core.get_property(d, "GPU_UARCH_VERSION"), "") or "")
            devs.append(e)
        inv["devices"] = devs
    except Exception as ex:   # inventory must still print on a broken install
        inv["openvino_error"] = repr(ex)
        inv["devices"] = []
    inv["openvino_telemetry"] = telemetry_status()
    models_root.mkdir(parents=True, exist_ok=True)
    inv["models_root"] = str(models_root)
    inv["disk_free_gb"] = round(shutil.disk_usage(models_root).free / 2**30, 1)
    if platform.system() == "Linux":
        inv["dri_render_nodes"] = sorted(str(p) for p in Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").exists() else []
        inv["kernel"] = platform.release()
    return inv


def _cpu_name() -> str:
    if platform.system() == "Linux":
        for line in _try(lambda: Path("/proc/cpuinfo").read_text().splitlines(), []):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    if platform.system() == "Windows":
        out = _try(lambda: subprocess.run(["powershell", "-NoProfile", "-Command",
                                           "(Get-CimInstance Win32_Processor).Name"], capture_output=True,
                                          text=True, timeout=20).stdout.strip())
        if out:
            return out
    return platform.processor()


def telemetry_status() -> dict:
    """OpenVINO's telemetry (openvino_telemetry, Google Analytics 4) sends an event on `import openvino` unless its
    consent file holds "0" or CI=true is set (openvino_telemetry/utils/opt_in_checker.py; found 2026-11-02).
    Reports the state without importing openvino and without changing anything."""
    if platform.system() == "Windows":
        f = Path(os.path.expandvars("$LOCALAPPDATA")) / "Intel Corporation" / "openvino_telemetry"
    else:
        f = Path.home() / "intel" / "openvino_telemetry"
    content = _try(lambda: f.read_text().strip()) if f.is_file() else None
    ci = os.environ.get("CI", "").lower() == "true"
    sends = not ci and content != "0"
    return {"consent_file": str(f), "consent_file_content": content, "env_CI_true": ci, "sends_on_import": sends,
            "turn_off": "opt_in_out --opt_out (writes 0 to the consent file), or CI=true for one process"}


def pick_gpu(inv: dict) -> str | None:
    """The Intel discrete GPU with the most memory (the B70 when present), else an Intel iGPU. OpenVINO can also list
    a non-Intel card through OpenCL (the laptop's NVIDIA RTX 4050 shows up as GPU.1); that is not a supported
    target, so non-Intel GPUs are only used when nothing else exists."""
    gpus = [d for d in inv.get("devices", []) if d["id"].startswith("GPU")]
    if not gpus:
        return None
    intel = [d for d in gpus if "intel" in (d.get("name") or "").lower()]
    gpus = intel or gpus
    disc = [d for d in gpus if "discrete" in d.get("type", "").lower() or "dGPU" in d.get("type", "")]
    pool = disc or gpus
    return max(pool, key=lambda d: d.get("memory_gb") or 0)["id"]


# ---- models -----------------------------------------------------------------------------------------------------
def ensure_model(repo: str, size_gb: float, models_root: Path, download: bool) -> dict:
    d = models_root / repo.split("/")[1]
    ok = (d / "openvino_language_model.xml").exists() and (d / "openvino_language_model.bin").exists()
    info = {"repo": repo, "dir": str(d), "present": ok, "expected_size_gb": size_gb}
    if ok:
        return info
    if not download:
        info["error"] = "not on disk and --no-download given"
        return info
    free = shutil.disk_usage(models_root).free / 2**30
    if free < size_gb * 1.15 + 2:
        info["error"] = f"needs about {size_gb * 1.15 + 2:.0f} GB free at {models_root}, has {free:.1f} GB"
        return info
    from huggingface_hub import snapshot_download   # reads HF_TOKEN from the environment if set; never printed
    print(f"downloading {repo} ({size_gb} GB) to {d}", flush=True)
    t = time.time()
    snapshot_download(repo, local_dir=str(d))
    info.update(present=(d / "openvino_language_model.xml").exists(), download_s=round(time.time() - t))
    return info


# ---- memory sampling --------------------------------------------------------------------------------------------
class PeakMemory:
    """Samples once a second while a model runs: this process's resident RAM (psutil), and GPU memory where the OS
    exposes it per process: Linux DRM fdinfo (drm-total-vram* / drm-resident-* lines of the xe / i915 drivers),
    Windows performance counters (GPU Process Memory: dedicated and shared). On an integrated GPU the model lives in
    shared system memory, so the RAM and shared numbers are the ones that matter there."""

    def __init__(self):
        import psutil
        self.p = psutil.Process()
        self.peak = {"process_rss_gb": 0.0}
        self._stop = threading.Event()
        self._win = None
        self._t = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        if platform.system() == "Windows":
            self._start_windows_counter()
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=5)
        self._sample()
        if self._win:
            self._win.terminate()
        self.peak = {k: round(v, 2) for k, v in self.peak.items()}

    def _bump(self, k, v_gb):
        if v_gb is not None:
            self.peak[k] = max(self.peak.get(k, 0.0), v_gb)

    def _sample(self):
        self._bump("process_rss_gb", self.p.memory_info().rss / 2**30)
        if platform.system() == "Linux":
            vram = self._linux_fdinfo()
            for k, v in vram.items():
                self._bump(k, v)

    def _run(self):
        while not self._stop.wait(1.0):
            _try(self._sample)

    def _linux_fdinfo(self) -> dict:
        """Sum per-client DRM memory keys over this process's DRM file descriptors (KiB in fdinfo)."""
        tot: dict[str, float] = {}
        seen = set()
        for f in Path(f"/proc/{os.getpid()}/fdinfo").glob("*"):
            txt = _try(lambda: f.read_text(), "")
            if "drm-driver" not in txt:
                continue
            cid = re.search(r"drm-client-id:\s*(\d+)", txt)
            if cid and cid.group(1) in seen:
                continue
            if cid:
                seen.add(cid.group(1))
            for m in re.finditer(r"^(drm-(?:total|resident)-(?:vram\d*|gtt|system\d*|memory)):\s*(\d+)\s*(KiB|MiB|B)?",
                                 txt, flags=re.M):
                mult = {"KiB": 1 / 2**20, "MiB": 1 / 2**10, "B": 1 / 2**30, None: 1 / 2**20}[m.group(3)]
                k = "gpu_" + m.group(1).replace("drm-", "").replace("-", "_") + "_gb"
                tot[k] = tot.get(k, 0.0) + int(m.group(2)) * mult
        return tot

    def _start_windows_counter(self):
        pid = os.getpid()
        ps = ("$c='\\GPU Process Memory(pid_%d_*)\\Dedicated Usage','\\GPU Process Memory(pid_%d_*)\\Shared Usage';"
              "Get-Counter -Counter $c -SampleInterval 1 -Continuous | ForEach-Object {"
              "$d=($_.CounterSamples | Where-Object {$_.Path -like '*dedicated usage'} | Measure-Object CookedValue -Sum).Sum;"
              "$s=($_.CounterSamples | Where-Object {$_.Path -like '*shared usage'} | Measure-Object CookedValue -Sum).Sum;"
              "Write-Output \"$d $s\" }") % (pid, pid)
        try:
            self._win = subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            self._win = None
            return

        def reader():
            for line in self._win.stdout:
                parts = line.split()
                if len(parts) == 2:
                    _try(lambda: self._bump("gpu_dedicated_gb", float(parts[0]) / 2**30))
                    _try(lambda: self._bump("gpu_shared_gb", float(parts[1]) / 2**30))
        threading.Thread(target=reader, daemon=True).start()


# ---- one model --------------------------------------------------------------------------------------------------
def isolate_outputs() -> Path:
    """Send this script's reads, runs and baseline answers to data/out/bench_b70/ (gitignored). With cache off the
    pipeline still writes each fresh read into its cache; without this, a bench run would overwrite the cached reads
    (and their timings) that eval.run_eval --score-only turns into the README numbers. Runtime override only; the
    pipeline code is not changed."""
    import eval.baseline as baseline
    from ledger_room import pipeline
    bench_out = pipeline.DATA / "out" / "bench_b70"
    pipeline.OUT = bench_out
    baseline.OUT = bench_out
    return bench_out


def subset_inbox(n: int) -> str:
    """Copy the first n held-out documents into data/out/bench_subset (gitignored) and return its set name.
    The frozen held-out inbox itself is never touched."""
    from ledger_room import pipeline
    clients, docs = pipeline.load_inbox(SET)
    name = "out/bench_subset"
    base = pipeline.DATA / name
    if base.exists():
        shutil.rmtree(base)
    (base / "clients").mkdir(parents=True)
    shutil.copy2(pipeline.DATA / SET / "clients.json", base / "clients.json")
    for d in docs[:n]:
        dst = base / "clients" / d["client_id"] / d["month"] / d["path"].name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(d["path"], dst)
    return name


def run_model(model_dir: Path, device: str, set_name: str, skip_baseline: bool) -> dict:
    from eval.baseline import run_baseline
    from eval.read_accuracy import score_reads
    from eval.run_eval import load_expected, score, secs
    from ledger_room import pipeline, rules
    from ledger_room.reader import Reader

    exp_all = load_expected(SET)
    _, docs = pipeline.load_inbox(set_name)
    ids = {d["doc_id"] for d in docs}
    exp = {**exp_all, "docs": [d for d in exp_all["docs"] if d["doc_id"] in ids]}
    expd = {d["doc_id"]: set(d["issues"]) for d in exp["docs"]}
    out: dict = {"model": model_dir.name, "device_requested": device, "n_docs": len(docs)}
    with PeakMemory() as mem:
        t = time.time()
        reader = Reader(str(model_dir), device)
        out.update(device=reader.device, load_s=round(time.time() - t, 1), thinking=reader.thinking)
        print(f"[{model_dir.name}] loaded on {reader.device} in {out['load_s']} s", flush=True)
        prog = lambda tag: (lambda i, n, did, r: print(f"  [{tag} {i}/{n}] {did} "   # noqa: E731
                                                        f"{(r.get('timing') or {}).get('total_s', r.get('seconds'))} s",
                                                        flush=True))
        t0 = time.time()
        run = pipeline.audit(set_name, reader, run_id=f"b70-{model_dir.name}-{datetime.now():%Y%m%d-%H%M%S}",
                             cache=False, progress=prog("ledger"), model_id=model_dir.name)
        wall = time.time() - t0
        pred: dict[str, set] = {}
        for e in run["exceptions"]:
            if e["rule_id"] in rules.ISSUE_TYPES:
                pred.setdefault(e["doc_id"], set()).add(e["rule_id"])
        per_doc = [d["timing"].get("total_s") for d in run["docs"]]
        reads = {d["doc_id"]: d for d in run["docs"]}
        held = {e["doc_id"] for e in run["exceptions"] if e["rule_id"] == "AMOUNT_MISMATCH"}
        sp = secs(per_doc)
        out["ledger_room"] = {**score(pred, expd), "sec_per_doc": sp,
                              "sec_per_doc_excluding_first": secs(per_doc[1:]) if len(per_doc) > 1 else None,
                              "wall_s": round(wall, 1), "run_id": run["summary"]["run_id"]}
        out["docs_per_hour"] = round(3600 / sp["mean"]) if sp and sp["mean"] else None
        out["reading"] = score_reads(reads, exp, held=held)
        if not skip_baseline:
            base = run_baseline(set_name, reader, cache=False, progress=prog("pure"), model_id=model_dir.name)
            bp = {k: set(v["issues"]) for k, v in base.items()}
            out["pure_model"] = {**score(bp, expd), "sec_per_doc": secs([v["seconds"] for v in base.values()]),
                                 "unparsed_replies": sum(1 for v in base.values() if not v["parsed"])}
        del reader
        gc.collect()
    out["peak_memory"] = mem.peak
    return out


# ---- targets ----------------------------------------------------------------------------------------------------
def judge(results: dict[str, dict], binding: bool) -> list[dict]:
    """One row per README target and model. pass is True / False, or None when the number is missing."""
    rows = []
    r9 = results.get("Qwen3.5-9B-int4-ov")

    def add(model, target, threshold, value, ok, note=""):
        rows.append({"model": model, "target": target, "threshold": threshold, "value": value,
                     "pass": ok, "binding": binding, "note": note})

    for model, r in results.items():
        t = TARGETS.get(model) or TARGETS["Qwen3.5-9B-int4-ov"]   # dry run: 9B targets, not binding
        tag = model if model in TARGETS else f"{model} (judged with the 9B targets, dry run only)"
        if "error" in r:
            add(tag, "run", None, None, False, r["error"])
            continue
        fer = (r.get("reading") or {}).get("field_error_rate")
        if "key_fields_wrong_max" in t:
            add(tag, "Key fields wrong, held-out", f"<= {t['key_fields_wrong_max']} (laptop 9B 20/337 + 1 point)",
                fer, None if fer is None else fer <= t["key_fields_wrong_max"])
        else:
            if r9 and "reading" in r9:
                ref, src = r9["reading"]["field_error_rate"], "B70 9B, same run"
            else:
                ref, src = round(LAPTOP_9B_FIELDS_WRONG / LAPTOP_9B_FIELDS_COMPARED, 4), "laptop 9B (9B not run here)"
            add(tag, "Key fields wrong, held-out", f"< {ref} ({src})", fer, None if fer is None else fer < ref)
        f1 = r["ledger_room"]["micro"]["f1"]
        add(tag, "Ledger Room micro F1, held-out", f">= {t['f1_min']}", f1, None if f1 is None else f1 >= t["f1_min"])
        sp = (r["ledger_room"].get("sec_per_doc") or {}).get("mean")
        add(tag, "Read time per document, whole pipeline (mean s)", f"<= {t['sec_per_doc_pass']}", sp,
            None if sp is None else sp <= t["sec_per_doc_pass"])
        if "sec_per_doc_goal" in t:
            add(tag, "Read time per document, goal (mean s)", f"<= {t['sec_per_doc_goal']}", sp,
                None if sp is None else sp <= t["sec_per_doc_goal"], "goal, not the pass line")
        pm = r.get("pure_model")
        add(tag, "Pure-model baseline with the same model", "reported next to Ledger Room",
            pm["micro"]["f1"] if pm else None, pm is not None)
    return rows


# ---- main -------------------------------------------------------------------------------------------------------
def default_models_root() -> Path:
    if os.environ.get("LEDGER_MODELS_ROOT"):
        return Path(os.environ["LEDGER_MODELS_ROOT"])
    if platform.system() == "Windows" and Path("E:/models").exists():
        return Path("E:/models")
    return Path.home() / "models"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Stage II B70 measurement (see module docstring)")
    ap.add_argument("--dry-run", action="store_true", help="laptop check: 2B, GPU (iGPU), 3 documents")
    ap.add_argument("--inventory-only", action="store_true")
    ap.add_argument("--models", default=None, help="comma list of 9b,27b,2b (default 9b,27b; dry run 2b)")
    ap.add_argument("--models-root", default=None, help="folder holding the model folders (env LEDGER_MODELS_ROOT)")
    ap.add_argument("--device", default=None, help="OpenVINO device (default: the discrete GPU with most memory)")
    ap.add_argument("--n", type=int, default=None, help="first N held-out documents (default all 47; dry run 3)")
    ap.add_argument("--skip-baseline", action="store_true")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    models_root = Path(a.models_root) if a.models_root else default_models_root()
    inv = inventory(models_root)
    print(json.dumps(inv, ensure_ascii=False, indent=1), flush=True)
    if a.inventory_only:
        return 0
    from ledger_room.pipeline import git_rev

    keys = (a.models or ("2b" if a.dry_run else "9b,27b")).split(",")
    n = a.n if a.n is not None else (3 if a.dry_run else None)
    device = a.device or pick_gpu(inv) or "CPU"
    bench_out = isolate_outputs()
    set_name = subset_inbox(n) if n else SET
    out_path = ROOT / "eval" / (a.out or ("results_b70_dryrun.json" if a.dry_run else "results_b70.json"))
    gpu_name = next((d.get("name") for d in inv["devices"] if d["id"] == device), None)
    on_b70 = bool(gpu_name and "B70" in gpu_name)
    binding = on_b70 and not a.dry_run and n is None
    doc = {
        "synthetic": True,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "code_rev": git_rev(), "dry_run": a.dry_run, "set": SET, "docs_used": n or "all",
        "device": device, "device_name": gpu_name, "measured_on_b70": on_b70,
        "targets_binding": binding,
        "targets_source": "README.md, 'Stage II targets on the B70' (frozen 2026-10-14)",
        "time_definition": "OCR + model + zoomed re-reads for one document, averaged over the set (fresh reads, cache off)",
        "outputs": str(bench_out), "inventory": inv, "models": {}, "results": {},
    }
    for k in keys:
        repo, size = MODELS[k.strip().lower()]
        info = ensure_model(repo, size, models_root, download=not a.no_download)
        doc["models"][repo] = info
        mid = repo.split("/")[1]
        if not info["present"]:
            doc["results"][mid] = {"error": info.get("error", "model missing")}
            continue
        try:
            doc["results"][mid] = run_model(Path(info["dir"]), device, set_name, a.skip_baseline)
        except Exception as ex:   # record and go on to the next model (e.g. out of GPU memory)
            doc["results"][mid] = {"error": f"{type(ex).__name__}: {ex}"[:500]}
        out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")   # keep partial results
    doc["targets"] = judge(doc["results"], binding)
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {out_path}")
    for t in doc["targets"]:
        mark = {True: "PASS", False: "FAIL", None: "n/a "}[t["pass"]]
        print(f"  {mark}  {t['model']:<28} {t['target']:<50} {t['value']!s:<8} {t['threshold']}")
    if not binding:
        print("  (not binding: " + ("dry run" if a.dry_run else "not on a B70" if not on_b70 else "subset of documents") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
