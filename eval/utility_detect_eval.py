"""Utility-bill recognition (rules.utility_bill): hits on a private real set and false positives on the public sets.

    python -I eval/utility_detect_eval.py --private-key E:\\ledger-private\\mom\\expected_mom.json \\
        --private-crops E:\\ledger-private\\mom\\crops --private-out E:\\ledger-private\\mom\\utility_eval \\
        --out eval/results_utility_detect.json

Private set: an answer key with one entry per voucher crop ("crop", "id", "kind"; kind "utility_bill" is the truth).
OCR lines are cached in --private-out/ocr_cache.json and the per-document result goes to --private-out/per_doc.json;
only the counts (n, tp, fp, fn) go into --out. No text, name or number from the private set is written to the repo.

Public sets (data/real_tw, real_tw2, heldout, main, blind3, hard): every image is OCR'd (cache in data/out, gitignored)
and the read fields saved in data/out/cache by earlier runs are added when present (the pipeline passes OCR text plus
the fields' seller name, note and items). Expected: 0 recognitions, as none of these documents is a utility bill.

The keywords were written after looking at the private set: label its numbers "after the fix".
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PUBLIC_SETS = ("real_tw", "real_tw2", "heldout", "main", "blind3", "hard")


def _ocr_text(path: Path, cache: dict, ck: str) -> str:
    if ck not in cache:
        from ledger_room import ocr
        cache[ck] = [l.text for l in ocr.read_lines(path)]
    lines = cache[ck]
    return " ".join(l["text"] if isinstance(l, dict) else l for l in lines)


def private(key_path: Path, crops: Path, out_dir: Path) -> dict:
    from ledger_room import rules
    key = json.loads(key_path.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    cp = out_dir / "ocr_cache.json"
    cache = json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {}
    tp = fp = fn = 0
    per_doc, misses, false_hits = [], [], []
    for d in key["docs"]:
        got = rules.utility_bill(_ocr_text(crops / d["crop"], cache, d["crop"]))
        truth = d["kind"] == "utility_bill"
        tp += got and truth
        fp += got and not truth
        fn += truth and not got
        if truth and not got:
            misses.append(d["id"])
        if got and not truth:
            false_hits.append(d["id"])
        per_doc.append({"id": d["id"], "kind": d["kind"], "recognized": got})
    cp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    agg = {"n": len(key["docs"]), "utility_bills": sum(d["kind"] == "utility_bill" for d in key["docs"]),
           "tp": tp, "fp": fp, "fn": fn}
    (out_dir / "per_doc.json").write_text(json.dumps({**agg, "misses": misses, "false_hits": false_hits,
                                                      "docs": per_doc}, ensure_ascii=False, indent=1), encoding="utf-8")
    return agg


def _cached_fields(sha: str) -> list[dict]:
    from ledger_room import pipeline
    out = []
    for p in (pipeline.OUT / "cache").glob(f"{sha[:24]}_*.json"):
        if p.stem.endswith("_raw") or "_raw_" in p.stem:
            continue
        try:
            f = json.loads(p.read_text(encoding="utf-8")).get("fields")
        except (OSError, ValueError):
            continue
        if isinstance(f, dict):
            out.append(f)
    return out


def public(sets=PUBLIC_SETS) -> dict:
    from ledger_room import pipeline, rules
    cp = pipeline.OUT / "utility_detect_ocr_cache.json"
    cache = json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {}
    res = {}
    for s in sets:
        base = ROOT / "data" / s / "clients"
        imgs = sorted(p for p in base.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
        hits, with_fields = [], 0
        for p in imgs:
            sha = pipeline.sha256_file(p)
            fields = _cached_fields(sha)
            with_fields += bool(fields)
            text = _ocr_text(p, cache, sha) + " " + " ".join(rules.fields_text(f) for f in fields)
            if rules.utility_bill(text):
                hits.append(str(p.relative_to(ROOT / "data")).replace("\\", "/"))
        res[s] = {"n": len(imgs), "recognized_as_utility_bill": len(hits), "files": hits,
                  "with_saved_read_fields": with_fields}
        cp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--private-key")
    ap.add_argument("--private-crops")
    ap.add_argument("--private-out")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    from ledger_room import pipeline
    res = {"base_code_rev": pipeline.git_rev() + " (measured on the working tree on top of this revision)",
           "recognizer": "ledger_room/rules.py utility_bill(): a utility's name AND a bill marker in the OCR text "
                         "(+ read fields), simplified characters folded; never on a slip with invoice-proof words "
                         "(證明聯, 統一發票)"}
    if a.private_key:
        res["private_real_set"] = {**private(Path(a.private_key), Path(a.private_crops), Path(a.private_out)),
                                   "after_fix": True, "note": "keywords written after seeing this private set",
                                   "text": "OCR only (no model reads exist for this set)"}
    res["public_sets_false_positive_check"] = public()
    txt = json.dumps(res, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(txt + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
