"""Two-card tiered reading vs the first reader alone, per data set -> eval/results_tiered.json

  python -m eval.bench_tiered --set heldout --set main --set private_real      # B70 host: 9B on GPU.0, 27B on GPU.1
  python -m eval.bench_tiered --set hard                                       # data/hard + eval/expected_hard.json
  python -m eval.bench_tiered --dry-run --set heldout --n 4 --first 2b --second 27b --second-fallback 2b

For each set, both modes read every document fresh (cache off), then the same rules run:
  first_only  the first reader (default 9B on GPU.0) alone
  tiered      the first reader reads everything; documents it is unsure about (ledger_room/tiered.py,
              escalation_reasons, fixed before any run) are re-read by the second reader (default 27B on GPU.1) at
              the same time on the other card; code merges the two readings, then the rules decide.

Per mode: documents a person must open (any hold or review exception, eval.run_eval.held_docs), key fields wrong
against the answer key (eval.read_accuracy), rule micro F1 (eval.run_eval.score), wall time of the whole batch and
documents per hour, busy seconds of each card's worker, documents sent to the second reader.

A set without eval/expected_<set>.json (private_real) gets counts and times only: no document ids, file names or
field values are written. measured_on_b70 is true only for a run on two B70 cards, not a dry run, with every
document of the set read and no reader error. The real results file keeps sets from earlier runs (each set carries
its own date, code revision, devices and models); a dry run writes eval/results_tiered_dryrun.json instead.
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODEL_DIRS = {"2b": "Qwen3.5-2B-int4-ov", "4b": "Qwen3.5-4B-int4-ov", "9b": "Qwen3.5-9B-int4-ov",
              "27b": "Qwen3.5-27B-int4-ov"}


def resolve_model(key: str, models_root: Path) -> Path:
    p = Path(key)
    if p.is_absolute() or p.exists():
        return p
    return models_root / MODEL_DIRS.get(key.lower(), key)


def model_present(d: Path) -> bool:
    return (d / "openvino_language_model.xml").exists()


def isolate_outputs() -> Path:
    """Reads, runs and caches of this script go to data/out/bench_tiered/ (gitignored), so the cached reads behind
    the README numbers are never overwritten. Runtime override only."""
    from ledger_room import pipeline
    out = pipeline.DATA / "out" / "bench_tiered"
    pipeline.OUT = out
    return out


def subset_inbox(set_name: str, n: int) -> str:
    """First n documents of a set copied under data/out/bench_tiered/subset/<set>; the inbox itself is not touched."""
    from ledger_room import pipeline
    _, docs = pipeline.load_inbox(set_name)
    rel = f"out/bench_tiered/subset/{set_name}"
    base = pipeline.DATA / rel
    if base.exists():
        shutil.rmtree(base)
    (base / "clients").mkdir(parents=True)
    shutil.copy2(pipeline.DATA / set_name / "clients.json", base / "clients.json")
    for d in docs[:n]:
        dst = base / "clients" / d["client_id"] / d["month"] / d["path"].name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(d["path"], dst)
    return rel


def expected_for(set_name: str, doc_ids: set[str]) -> dict | None:
    p = ROOT / "eval" / f"expected_{set_name}.json"
    if not p.exists():
        return None
    exp = json.loads(p.read_text(encoding="utf-8"))
    return {**exp, "docs": [d for d in exp["docs"] if d["doc_id"] in doc_ids]}


def run_mode(tr, inbox: str, exp: dict | None, private: bool, tag: str) -> dict:
    from eval.read_accuracy import score_reads
    from eval.run_eval import held_docs, score
    from ledger_room import pipeline, rules

    def prog(i, n, did, r):
        t = (r.get("timing") or {}).get("total_s")
        esc = " -> second reader" if (r.get("tier") or {}).get("escalated") else ""
        print(f"  [{tag} {i}/{n}] {'(private)' if private else did} {t} s{esc}", flush=True)

    t0 = time.perf_counter()
    run = pipeline.audit(inbox, tr, run_id=f"tiered-{tag}-{datetime.now():%Y%m%d-%H%M%S}", cache=False,
                         progress=prog, read_fn=tr.read_docs)
    wall = time.perf_counter() - t0
    st = dict(tr.stats or {})
    n = run["summary"]["n_docs"]
    held = held_docs(run)
    review_by_field: dict[str, int] = {}
    for e in run["exceptions"]:
        if e["rule_id"] == "READ_REVIEW":
            for f in e.get("evidence_fields") or []:
                review_by_field[f] = review_by_field.get(f, 0) + 1
    out = {
        "docs": n,
        "docs_for_person": held,
        "docs_for_person_rate": round(held / n, 3) if n else None,
        "exceptions_by_rule": {k: v for k, v in sorted(_count(e["rule_id"] for e in run["exceptions"]).items())},
        "read_review_by_field": review_by_field,
        "wall_s": round(wall, 1),
        "docs_per_hour": round(3600 * n / wall) if wall > 0 else None,
        "read_wall_s": st.get("read_wall_s"),
        "busy_s": {"card_first": (st.get("busy_s") or {}).get("first"),
                   "card_second": (st.get("busy_s") or {}).get("second")},
        "model_call_s": st.get("model_call_s"),
        "overlap_s": st.get("overlap_s"),
        "first_done_s": st.get("first_done_s"),
        "second_tail_s": st.get("second_tail_s"),
        "unsure_docs": st.get("unsure_docs"),
        "sent_to_second": st.get("escalated"),
        "escalation_reasons": st.get("reasons"),
        "fields_taken_from_second": st.get("fields_taken_from_second"),
        "second_errors": len(st.get("second_errors") or []),
    }
    if exp is not None:
        expd = {d["doc_id"]: set(d["issues"]) for d in exp["docs"]}
        pred: dict[str, set] = {}
        for e in run["exceptions"]:
            if e["rule_id"] in rules.ISSUE_TYPES:
                pred.setdefault(e["doc_id"], set()).add(e["rule_id"])
        amount_held = {e["doc_id"] for e in run["exceptions"] if e["rule_id"] == "AMOUNT_MISMATCH"}
        reading = score_reads({d["doc_id"]: d for d in run["docs"]}, exp, held=amount_held)
        sc = score(pred, expd)
        out["key_fields_wrong"] = reading["fields_wrong"]
        out["key_fields_compared"] = reading["fields_compared"]
        out["key_field_error_rate"] = reading["field_error_rate"]
        out["key_fields_per_field"] = reading["per_field"]
        out["rule_f1"] = sc["micro"]["f1"]
        out["rule_micro"] = sc["micro"]
        out["clean_docs_flagged"] = sc["clean_docs_flagged"]
        out["escalated_docs"] = st.get("escalated_docs")
        out["run_id"] = run["summary"]["run_id"]
    else:
        out["key_fields_wrong"] = out["rule_f1"] = None
        out["note"] = "no answer key in the repository: counts and times only"
    if st.get("second_note"):
        out["second_note"] = st["second_note"]
    out["_complete"] = n > 0 and not st.get("second_errors")
    return out


def _count(it):
    c: dict = {}
    for x in it:
        c[x] = c.get(x, 0) + 1
    return c


def main(argv=None) -> int:
    from eval import bench_b70 as B

    ap = argparse.ArgumentParser(description="Tiered two-card reading (see module docstring)")
    ap.add_argument("--set", action="append", default=None,
                    help="data set under data/ (repeat or comma list); default heldout,main")
    ap.add_argument("--first", default="9b", help="first reader: 2b, 4b, 9b, 27b or a model folder (default 9b)")
    ap.add_argument("--second", default="27b", help="second reader (default 27b)")
    ap.add_argument("--second-fallback", default=None,
                    help="model to use when the second one is not on disk (e.g. 9b); without it the tiered mode "
                         "is skipped and the results file says so")
    ap.add_argument("--first-device", default=None, help="default GPU.0 (dry run: CPU)")
    ap.add_argument("--second-device", default=None, help="default GPU.1 (dry run: GPU.0 if present, else CPU)")
    ap.add_argument("--models-root", default=None)
    ap.add_argument("--n", type=int, default=None, help="first N documents of each set (dry run default 3)")
    ap.add_argument("--dry-run", action="store_true", help="laptop plumbing check; writes results_tiered_dryrun.json")
    ap.add_argument("--escalate-all", action="store_true",
                    help="dry run only: send every document to the second reader, to check the two-card path")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    if a.escalate_all and not a.dry_run:
        ap.error("--escalate-all is only allowed with --dry-run")

    import ledger_room  # noqa: F401  (sets CI=true before openvino is imported)
    from ledger_room import tiered
    from ledger_room.pipeline import git_rev, load_inbox
    from ledger_room.reader import Reader

    sets = [s.strip() for v in (a.set or ["heldout,main"]) for s in v.split(",") if s.strip()]
    models_root = Path(a.models_root) if a.models_root else B.default_models_root()
    inv = B.inventory(models_root)
    devs = {d["id"]: d.get("name") for d in inv.get("devices", [])}
    first_dev = a.first_device or ("CPU" if a.dry_run else "GPU.0")
    second_dev = a.second_device or (("GPU.0" if "GPU.0" in devs else "CPU") if a.dry_run else "GPU.1")
    n = a.n if a.n is not None else (3 if a.dry_run else None)
    bench_out = isolate_outputs()
    out_path = ROOT / "eval" / (a.out or ("results_tiered_dryrun.json" if a.dry_run else "results_tiered.json"))

    first_dir = resolve_model(a.first, models_root)
    if not model_present(first_dir):
        print(f"first model not found: {first_dir}", file=sys.stderr)
        return 2
    second_dir = resolve_model(a.second, models_root)
    second_info = {"requested": second_dir.name, "used": None, "substituted": False, "device": second_dev}
    if not model_present(second_dir):
        if a.second_fallback and model_present(resolve_model(a.second_fallback, models_root)):
            second_info.update(substituted=True,
                               note=f"{second_dir.name} not on disk at {models_root}; {a.second_fallback} used instead")
            second_dir = resolve_model(a.second_fallback, models_root)
        else:
            second_info["note"] = f"{second_dir.name} not on disk at {models_root}; tiered mode skipped"
            second_dir = None

    t = time.time()
    first = Reader(str(first_dir), first_dev)
    first_info = {"model": first.model_id, "device": first.device, "device_name": devs.get(first.device),
                  "load_s": round(time.time() - t, 1), "thinking": first.thinking}
    print(f"first reader {first.model_id} on {first.device} ({first_info['load_s']} s)", flush=True)
    second = None
    if second_dir is not None:
        t = time.time()
        second = Reader(str(second_dir), second_dev)
        second_info.update(used=second.model_id, device=second.device, device_name=devs.get(second.device),
                           load_s=round(time.time() - t, 1), thinking=second.thinking)
        print(f"second reader {second.model_id} on {second.device} ({second_info['load_s']} s)", flush=True)

    on_b70 = all("B70" in (devs.get(x) or "") for x in (first.device, second.device if second else second_dev))
    escalate = (lambda res: tiered.escalation_reasons(res) or ["escalate_all (dry run)"]) if a.escalate_all \
        else tiered.escalation_reasons

    prev = {}
    if not a.dry_run and out_path.exists():
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8")).get("sets", {})
        except Exception:
            prev = {}
    doc = {
        "synthetic_sets_only_unless_noted": True,
        "dry_run": a.dry_run,
        "escalate_all": a.escalate_all,
        "escalation_rule": tiered.ESCALATION_RULE_VERSION,
        "escalation_rule_text": tiered.escalation_reasons.__doc__.strip(),
        "merge_rule_text": " ".join(tiered.merge.__doc__.split()),
        "time_definition": "wall_s: whole batch (fresh reads, cache off, OCR + models + rules + evidence crops + log); "
                           "busy_s: seconds each card's worker spent reading (the first worker's time includes OCR on "
                           "the CPU); overlap_s: busy time of both workers beyond the read wall clock",
        "docs_for_person_definition": "documents with any hold or review exception (eval.run_eval.held_docs)",
        "outputs": str(bench_out),
        "inventory": {k: inv.get(k) for k in ("os", "host", "cpu", "ram_gb", "openvino", "openvino_genai", "devices")},
        "sets": prev,
    }

    def save():
        ok = [s for s in doc["sets"].values() if isinstance(s, dict)]
        doc["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        doc["measured_on_b70"] = bool(ok) and all(s.get("measured_on_b70") for s in ok)
        out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    for s in sets:
        private = s.startswith("private")
        try:
            _, docs_all = load_inbox(s)
        except FileNotFoundError as ex:
            doc["sets"][s] = {"error": f"set not found: {ex}", "measured_on_b70": False}
            save()
            continue
        inbox = subset_inbox(s, n) if n else s
        _, docs = load_inbox(inbox)
        exp = None if private else expected_for(s, {d["doc_id"] for d in docs})
        rec = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "code_rev": git_rev(),
            "dry_run": a.dry_run, "escalate_all": a.escalate_all, "docs_in_set": len(docs_all), "docs_used": len(docs),
            "answer_key": f"eval/expected_{s}.json" if exp is not None else None,
            "first": first_info, "second": dict(second_info),
        }
        print(f"\n== {s}: {len(docs)} documents ==", flush=True)
        try:
            rec["first_only"] = run_mode(tiered.TieredReader(first, None, second_note="not used (first reader alone)",
                                                             escalate=escalate), inbox, exp, private, f"{s} first")
            if second is not None:
                rec["tiered"] = run_mode(tiered.TieredReader(first, second, escalate=escalate), inbox, exp, private,
                                         f"{s} tiered")
            else:
                rec["tiered"] = {"skipped": second_info.get("note")}
        except Exception as ex:   # record and go on (e.g. out of GPU memory)
            rec["error"] = f"{type(ex).__name__}: {ex}"[:500]
        done = {m: isinstance(rec.get(m), dict) and rec[m].pop("_complete", False) for m in ("first_only", "tiered")}
        rec["complete"] = ("error" not in rec and len(docs) == len(docs_all) and second is not None
                           and done["first_only"] and done["tiered"])
        complete = rec["complete"]
        rec["measured_on_b70"] = bool(on_b70 and not a.dry_run and complete)
        if private:
            rec["private"] = "aggregates only; no document ids, file names or values"
        doc["sets"][s] = rec
        save()

    print(f"\nwrote {out_path}")
    for s in sets:
        r = doc["sets"].get(s, {})
        for m in ("first_only", "tiered"):
            x = r.get(m) or {}
            if "docs" in x:
                print(f"  {s:<14} {m:<10} person {x['docs_for_person']:>3}/{x['docs']:<4} fields wrong "
                      f"{x.get('key_fields_wrong')!s:<5} F1 {x.get('rule_f1')!s:<6} wall {x['wall_s']:>7} s "
                      f"{x['docs_per_hour']!s:>6}/h  sent {x.get('sent_to_second')}  busy {x['busy_s']}")
            elif x:
                print(f"  {s:<14} {m:<10} {x}")
        if r.get("error"):
            print(f"  {s:<14} error {r['error']}")
    print(f"  measured_on_b70: {doc['measured_on_b70']}" + ("" if on_b70 else " (not on two B70 cards)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
