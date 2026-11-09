"""Same host, same code, same first 12 held-out documents: the 9B on the host CPU, then the 9B on one B70.

    .venv/bin/python -m eval.bench_cpu_vs_b70                      # CPU then GPU.0 -> eval/results_same_host_cpu_vs_b70_live.json
    .venv/bin/python -m eval.bench_cpu_vs_b70 --devices CPU,GPU.1  # the other card

It runs the measurement behind eval/results_same_host_cpu_vs_b70.json (2026-11-07), which was made with two calls of
`eval.bench_b70 --models 9b --device {CPU|GPU.0} --n 12 --skip-baseline --no-download`, as one command: the same
run_model (fresh reads, cache off, OCR + model + zoomed re-reads per document), one line per document as it is read,
and one results file in the same format (default a new file, so the 2026-11-07 one is kept). bench_b70's own inventory print and Stage II target table are left out.
Synthetic data only.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval import bench_b70 as B  # noqa: E402

N = 12
MODEL_KEY = "9b"


def run_summary(r: dict) -> dict:
    lr, rd = r["ledger_room"], r["reading"]
    sp, sx = lr["sec_per_doc"], lr.get("sec_per_doc_excluding_first") or {}
    return {
        "device": r["device"],
        "sec_per_doc": {"mean": sp["mean"], "median": sp["median"], "n": sp["n"]},
        "sec_per_doc_excluding_first": {"mean": sx.get("mean"), "n": sx.get("n")},
        "docs_per_hour": r["docs_per_hour"], "wall_s": lr["wall_s"],
        "fields_wrong": rd["fields_wrong"], "fields_compared": rd["fields_compared"],
        "rule_micro": {k: lr["micro"][k] for k in ("tp", "fp", "fn", "f1")},
        "run_id": lr["run_id"],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="9B on the host CPU against the 9B on one B70 (see module docstring)")
    ap.add_argument("--devices", default="CPU,GPU.0", help="comma list of OpenVINO devices, run in this order")
    ap.add_argument("--n", type=int, default=N, help=f"first N held-out documents (default {N})")
    ap.add_argument("--models-root", default=None, help="folder holding the model folders (env LEDGER_MODELS_ROOT)")
    ap.add_argument("--out", default="eval/results_same_host_cpu_vs_b70_live.json", help="path, relative to the repo root")
    a = ap.parse_args(argv)

    import openvino as ov
    import psutil
    from ledger_room.pipeline import git_rev

    models_root = Path(a.models_root) if a.models_root else B.default_models_root()
    repo, size = B.MODELS[MODEL_KEY]
    info = B.ensure_model(repo, size, models_root, download=False)
    if not info["present"]:
        print(f"model missing: {info['dir']}", flush=True)
        return 1
    B.isolate_outputs()
    set_name = B.subset_inbox(a.n)
    devices = [d.strip() for d in a.devices.split(",") if d.strip()]
    core = ov.Core()
    names = {d: str(B._try(lambda d=d: core.get_property(d, "FULL_DEVICE_NAME"), "")) for d in devices}
    doc = {
        "what": f"Same host, same code, same {a.n} held-out synthetic documents: the 9B on "
                + " against the 9B on ".join(names[d] or d for d in devices) + ".",
        "command": ".venv/bin/python -m eval.bench_cpu_vs_b70 " + " ".join(argv if argv is not None else sys.argv[1:]),
        "host": {"cpu": B._cpu_name(), "cpu_logical": psutil.cpu_count(),
                 "ram_gb": round(psutil.virtual_memory().total / 2**30, 1), "openvino": ov.__version__},
        "device_names": names,
        "code_rev": git_rev(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runs": {},
    }
    print(f"{a.n} held-out documents, {repo.split('/')[1]}, fresh reads (cache off), code {doc['code_rev']}", flush=True)
    t0 = time.time()
    for d in devices:
        print(f"\n== {d}: {names[d]}", flush=True)
        r = B.run_model(Path(info["dir"]), d, set_name, skip_baseline=True)
        doc["runs"][d] = run_summary(r)
    doc["total_wall_s"] = round(time.time() - t0, 1)
    out = ROOT / a.out
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print("", flush=True)
    for d, s in doc["runs"].items():
        print(f"{d:<6} {s['sec_per_doc']['mean']} s per voucher, {s['docs_per_hour']} an hour, "
              f"{s['fields_wrong']}/{s['fields_compared']} fields wrong", flush=True)
    print(f"wrote {out.relative_to(ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
