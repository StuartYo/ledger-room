"""Real photos: Taiwanese vouchers from Wikimedia Commons (real_tw) and foreign receipts (CORD v2, Zenodo 13688441).

real_tw (41 photos in data/real_tw, answer key eval/expected_real_tw.json, frozen in eval/frozen_real_tw.json)
  python -I eval/real_photo_eval.py tw --ocr-only
  python -I eval/real_photo_eval.py tw --model-dir E:\\models\\Qwen3.5-2B-int4-ov --device CPU --limit 5      (dry run)
  python -I eval/real_photo_eval.py tw --model-dir DIR --device GPU --official --out eval/results_real_tw.json
  Add --set real_tw2 for the second blind set (data/real_tw2, eval/expected_real_tw2.json, eval/frozen_real_tw2.json).
  Three readers, scored field by field against the key:
    OCR only      eval/ocr_only.py: OCR + the same reconciliation code and rules, no model
    model only    the model's own transcription (the same reply Ledger Room gets), with format clean-up only
                  (date to YYYY-MM-DD, digits only for the UBN, letters + 8 digits for the invoice number). No checks,
                  so nothing is ever sent to a person.
    Ledger Room   OCR + model + the code that cross-checks them, then the rules. A photo is "sent to a person" when
                  any rule with severity hold or review fires on it (READ_REVIEW, AMOUNT_MISMATCH, NOT_A_VOUCHER,
                  BUYER_PRESUMED), the same definition as eval/real_check.py.
  Without --official a model run on real_tw needs --limit <= 5 and is marked dry_run. --official reads every photo
  afresh (no cached reads) and refuses to overwrite an earlier official entry for the same model.

real_intl (photos stay on F:, nothing from them is committed; see data/real_intl/SOURCES.md)
  python -I eval/real_photo_eval.py prepare-cord --src F:\\datasets\\receipts\\cord_v2 --dst F:\\datasets\\receipts\\cord_v2\\prepared
  python -I eval/real_photo_eval.py prepare-zenodo --src F:\\datasets\\receipts\\zenodo_13688441\\extracted --dst F:\\datasets\\receipts\\zenodo_13688441\\prepared
  python -I eval/real_photo_eval.py intl --root F:\\datasets\\receipts\\cord_v2\\prepared (--ocr-only | --model-dir DIR --device GPU) [--limit N]
  Total only. Ledger Room's own reader is not run on these receipts: its prompt and labels are for Taiwanese vouchers
  and its amount parser reads "60.000" (Indonesian thousands separator) as 60. What is tested is the idea behind its
  total check, written out here for any currency format: a total is accepted only when two printed sources agree.
    OCR only      the amount on the OCR row labelled TOTAL / GRAND TOTAL (not SUBTOTAL, TAX, CASH, CHANGE ...)
    model only    the model's answer to "what is the total", accepted as is
    OCR + model   accepted when both are the same amount; otherwise the receipt goes to a person
  Taiwanese rules (UBN checksum, invoice period, duplicates, Art. 19) do not apply and are not run.

prepare-cord needs pyarrow (pip install pyarrow); nothing else here does. Downloaded data is only read, never executed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:   # run as a script with python -I: the repository is not on sys.path by itself
    sys.path.insert(0, str(ROOT))

TW_SET = "real_tw"
TW_SETS = ("real_tw", "real_tw2")   # same layout, key and freeze format; pick one with --set
TW_FIELDS = ["invoice_no", "date", "seller_ubn", "total", "net", "tax"]
ONLY_WHEN_PRINTED = {"net", "tax"}   # scored only on vouchers that print an untaxed amount and a tax line
DRY_RUN_MAX = 5
OUT_DIR = ROOT / "data" / "out" / "real_photos"   # gitignored
LABEL_NOTE = "labels by Claude (a different model from the readers under test), from the image only"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------- real_tw

def load_truth(path: Path | None = None, set_name: str = TW_SET) -> dict:
    return json.loads((path or ROOT / "eval" / f"expected_{set_name}.json").read_text(encoding="utf-8"))


def check_frozen(set_name: str = TW_SET) -> None:
    """Refuse to score against a key or photos that changed after the freeze."""
    fr = json.loads((ROOT / "eval" / f"frozen_{set_name}.json").read_text(encoding="utf-8"))
    bad = [k for k in ("expected", "clients_json") if sha256(ROOT / fr[k]["path"]) != fr[k]["sha256"]]
    bad += [d for d, h in fr["images"].items() if sha256(ROOT / "data" / set_name / "clients" / d) != h]
    if bad:
        raise SystemExit(f"{set_name} changed after it was frozen: {bad[:5]}")


def model_only_fields(obj: dict) -> dict:
    """The model's transcription with format clean-up only."""
    from ledger_room import normalize as N
    from ledger_room import rules
    d = N.parse_date(obj.get("date"))
    ubn = N.digits(obj.get("seller_ubn"))
    return {"invoice_no": rules.norm_invoice_no(N.clean_str(obj.get("no"))), "date": d.isoformat() if d else None,
            "seller_ubn": ubn, "total": N.to_int(obj.get("total")) or None,
            "net": N.to_int(obj.get("net")), "tax": N.to_int(obj.get("tax"))}


def score_tw(truth_docs: list[dict], got: dict[str, dict], held: set[str] | None) -> dict:
    """got: file name -> fields. held: file names sent to a person (None: the reader has no way to send one).

    A field is scored when the key has a value for it (not "unreadable"; net and tax only when printed).
    A value the reader gives for a field the voucher does not print is counted apart (values_not_printed)."""
    per = {f: [0, 0] for f in TW_FIELDS}
    extra = Counter()
    wrong_docs, unreadable = set(), 0
    for t in truth_docs:
        g = got.get(t["file"]) or {}
        for f in TW_FIELDS:
            want = t.get(f)
            if want == "unreadable":
                unreadable += 1
                continue
            if want is None:
                if g.get(f) is not None and f not in ONLY_WHEN_PRINTED:
                    extra[f] += 1
                continue
            per[f][1] += 1
            if g.get(f) == want:
                per[f][0] += 1
            else:
                wrong_docs.add(t["file"])
    n_f = sum(v[1] for v in per.values())
    n_wrong = sum(v[1] - v[0] for v in per.values())
    files = [t["file"] for t in truth_docs]
    out = {
        "photos": len(files),
        "fields_scored": n_f, "fields_wrong": n_wrong,
        "field_error_rate": round(n_wrong / n_f, 3) if n_f else None,
        "by_field": {f: f"{v[1] - v[0]} wrong / {v[1]}" for f, v in per.items() if v[1]},
        "values_not_printed": dict(extra),
        "fields_unreadable_not_scored": unreadable,
        "photos_with_a_wrong_field": len(wrong_docs),
    }
    if held is None:
        out.update({"photos_sent_to_a_person": 0, "wrong_and_not_held": len(wrong_docs),
                    "note": "no checks: every value is accepted as read"})
    else:
        h = held & set(files)
        out.update({"photos_sent_to_a_person": len(h), "wrong_and_not_held": len(wrong_docs - h),
                    "held_with_all_fields_right": len(h - wrong_docs)})
    return out


def run_tw(a) -> dict:
    from ledger_room import pipeline, rules
    set_name = getattr(a, "set", None) or TW_SET
    check_frozen(set_name)
    T = load_truth(set_name=set_name)
    clients, docs = pipeline.load_inbox(set_name)
    keep = {t["file"] for t in T["docs"]}
    docs = [d for d in docs if d["path"].name in keep]
    if a.limit:
        docs = docs[: a.limit]
    files = {d["path"].name for d in docs}
    truth_docs = [t for t in T["docs"] if t["file"] in files]
    use_model = not a.ocr_only
    dry = bool(a.limit) or a.dry_run
    if use_model and not a.official and (not a.limit or a.limit > DRY_RUN_MAX):
        raise SystemExit(f"{set_name} with a model: --limit <= {DRY_RUN_MAX} (dry run) or --official (first full run)")
    if a.official and (a.limit or a.ocr_only):
        raise SystemExit("--official is the full model run: no --limit, no --ocr-only")

    def audit(reader, model_id, cache):
        reads = pipeline.read_docs(docs, reader, cache=cache, model_id=model_id,
                                   progress=lambda i, n, did, r: print(f"[{model_id} {i}/{n}] {did}", flush=True))
        rdocs = [pipeline.to_rule_doc(d, reads[d["doc_id"]]["fields"], reads[d["doc_id"]]["provenance"],
                                      reads[d["doc_id"]].get("ocr_lines"), reads[d["doc_id"]].get("raw")) for d in docs]
        ex = rules.run_rules(rdocs, clients)
        held = {Path(e.doc_id).name for e in ex if e.severity in ("hold", "review")}
        fields = {Path(k).name: v["fields"] for k, v in reads.items()}
        why = Counter(e.rule_id for e in ex if e.severity in ("hold", "review"))
        secs = [r["timing"].get("total_s") for r in reads.values() if r["timing"].get("total_s")]
        return reads, fields, held, why, (round(sum(secs) / len(secs), 2) if secs else None)

    res = {"set": set_name, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "code_rev": pipeline.git_rev(), "dry_run": dry, "labels": T["note"],
           "photos": len(docs), "photo_files": sorted(files) if dry else "all",
           "conditions": {}}
    from eval.ocr_only import MODEL_ID as OCR_ID, OcrOnlyReader
    _, f_ocr, held_ocr, why_ocr, s_ocr = audit(OcrOnlyReader(), OCR_ID, cache=not a.no_cache)
    res["conditions"]["ocr_only"] = {**score_tw(truth_docs, f_ocr, held_ocr), "hold_rules": dict(why_ocr),
                                     "sec_per_photo": s_ocr}
    if use_model:
        from ledger_room import normalize as N
        from ledger_room.reader import Reader
        reader = Reader(a.model_dir, a.device)
        res.update({"model": reader.model_id, "device": reader.device, "thinking": reader.thinking})
        # --official: fresh reads, so no cached dry-run read (possibly from another device) is reused
        reads, f_lr, held_lr, why_lr, s_lr = audit(reader, reader.model_id, cache=not (a.official or a.no_cache))
        f_mo = {}
        for d in docs:
            raw = pipeline.OUT / "cache" / f"{pipeline.cache_key(pipeline.sha256_file(d['path']), reader.model_id, raw=True)}.json"
            obj = N.parse_json_loose(json.loads(raw.read_text(encoding="utf-8"))["raw"]) or {}
            f_mo[d["path"].name] = model_only_fields(obj)
        res["conditions"]["model_only"] = score_tw(truth_docs, f_mo, None)
        res["conditions"]["ledger_room"] = {**score_tw(truth_docs, f_lr, held_lr), "hold_rules": dict(why_lr),
                                            "sec_per_photo": s_lr}
    if a.official:
        res["first_run"] = True
    if a.show_reads:   # per-photo values, for the dry run only (the key itself is in the repository anyway)
        res["reads"] = {f: {"ocr_only": {k: f_ocr[f].get(k) for k in TW_FIELDS},
                            **({"model_only": f_mo[f], "ledger_room": {k: f_lr[f].get(k) for k in TW_FIELDS},
                                "held": f in held_lr} if use_model else {})} for f in sorted(files)}
    return res


# ---------------------------------------------------------------- real_intl

def to_amount(s) -> Decimal | None:
    """'45,500' '60.000' 'Rp. 35.000' '1,234.50' '$56.58' -> Decimal. Groups of three after a separator are thousands;
    one or two digits after the last separator are decimals."""
    if s is None:
        return None
    t = re.sub(r"[^\d.,]", "", str(s)).strip(".,")
    if not t or not re.search(r"\d", t):
        return None
    m = re.fullmatch(r"(\d{1,3}(?:([.,])\d{3})*)(?:([.,])(\d{1,2}))?", t)
    if m and (m.group(3) is None or m.group(3) != m.group(2)):
        whole = re.sub(r"[.,]", "", m.group(1))
        return Decimal(whole + ("." + m.group(4) if m.group(4) else "")).normalize() + 0
    if re.fullmatch(r"\d+", t):
        return Decimal(t)
    m = re.fullmatch(r"(\d+)[.,](\d{1,2})", t)
    if m:
        return Decimal(f"{m.group(1)}.{m.group(2)}").normalize() + 0
    return None


_AMT = re.compile(r"(?<![\d])(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?![\d])")
TOTAL_LABEL = re.compile(r"grand\s*total|\btotal\b|\bjumlah\b|amount\s*due|balance\s*due|\btotal\s*bayar", re.I)
NOT_TOTAL = re.compile(r"sub\s*-?\s*total|total\s*(item|items|qty|quantity|disc|discount|tax|pajak|ppn|saving|hemat)|"
                       r"\b(tax|ppn|pb1|service|cash|tunai|change|kembali|tip|gratuity|discount|disc)\b", re.I)


def page_amounts(lines) -> set[Decimal]:
    out = set()
    for l in lines:
        for m in _AMT.finditer(l.text):
            v = to_amount(m.group(1))
            if v:
                out.add(v)
    return out


def ocr_total(lines) -> Decimal | None:
    """The rightmost amount on the last row labelled as the total (GRAND TOTAL first)."""
    def row_amount(lab):
        h = max(12, lab.box[3] - lab.box[1])
        cy = (lab.box[1] + lab.box[3]) / 2
        row = [lab] + sorted((l for l in lines if l is not lab and l.box[0] > lab.box[0]
                              and abs((l.box[1] + l.box[3]) / 2 - cy) < 0.8 * h), key=lambda l: l.box[0])
        vals = []
        for l in row:
            text = TOTAL_LABEL.sub(" ", l.text) if l is lab else l.text
            vals += [v for v in (to_amount(m.group(1)) for m in _AMT.finditer(text)) if v]
        return vals[-1] if vals else None

    labs = [l for l in sorted(lines, key=lambda l: l.box[1]) if TOTAL_LABEL.search(l.text) and not NOT_TOTAL.search(l.text)]
    grand = [l for l in labs if re.search(r"grand\s*total", l.text, re.I)]
    for lab in reversed(grand or labs):
        v = row_amount(lab)
        if v is not None:
            return v
    return None


INTL_PROMPT = """This photo shows one shop or restaurant receipt. OCR text lines of the photo:
{ocr}

What is the final total the customer has to pay on this receipt? Reply with that amount only, written as printed on the receipt, nothing else."""


def score_intl(rows: list[dict], key: str, held_key: str | None) -> dict:
    n = len(rows)
    right = sum(1 for r in rows if r[key] is not None and r[key] == r["truth"])
    wrong = [r for r in rows if r[key] != r["truth"]]
    out = {"receipts": n, "total_right": right, "total_wrong_or_missing": len(wrong),
           "error_rate": round(len(wrong) / n, 3) if n else None}
    if held_key is None:
        out.update({"sent_to_a_person": 0, "wrong_and_not_held": len(wrong)})
    else:
        held = [r for r in rows if r[held_key]]
        out.update({"sent_to_a_person": len(held),
                    "wrong_and_not_held": sum(1 for r in wrong if not r[held_key]),
                    "held_but_right": sum(1 for r in held if r["model"] is not None and r["model"] == r["truth"])})
    return out


def run_intl(a) -> dict:
    from ledger_room.ocr import Line, read_lines
    root = Path(a.root)
    meta = json.loads((root / "truth.json").read_text(encoding="utf-8"))
    items = [x for x in meta["receipts"] if x.get("total") is not None]
    if a.split:
        items = [x for x in items if x["split"] in a.split.split(",")]
    if a.limit:
        items = items[: a.limit]
    reader = None
    if not a.ocr_only:
        from PIL import Image
        from ledger_room.reader import Reader
        reader = Reader(a.model_dir, a.device)
    mid = reader.model_id if reader else "ocr-only"
    cache_p = OUT_DIR / f"intl_{meta['dataset']}_{mid}.jsonl"
    cache_p.parent.mkdir(parents=True, exist_ok=True)
    ph = hashlib.sha256(INTL_PROMPT.encode("utf-8")).hexdigest()[:10]
    cache = {}
    if cache_p.exists() and not a.no_cache:
        for line in cache_p.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            cache[(r["sha256"], r["prompt"])] = r
    rows = []
    for i, x in enumerate(items):
        p = root / x["file"]
        h = sha256(p)
        c = cache.get((h, ph))
        if c is None:
            t = time.time()
            lines = read_lines(p)
            c = {"file": x["file"], "sha256": h, "prompt": ph, "ocr_lines": [[l.id, l.text, list(l.box), l.conf] for l in lines],
                 "ocr_s": round(time.time() - t, 2)}
            if reader is not None:
                ocr = "\n".join(f"{l.id}: {l.text}" for l in lines) or "(no text found)"
                txt, tm = reader.generate(INTL_PROMPT.format(ocr=ocr), Image.open(p), max_new_tokens=24)
                c.update({"model_reply": txt.strip()[:80], "vlm_s": tm["vlm_s"]})
            with cache_p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        lines = [Line(l[0], l[1], tuple(l[2]), l[3]) for l in c["ocr_lines"]]
        o = ocr_total(lines)
        m = to_amount(c.get("model_reply")) if reader is not None else None
        rows.append({"file": x["file"], "truth": to_amount(x["total"]), "ocr": o, "model": m,
                     "held": not (o is not None and o == m), "model_on_page": m in page_amounts(lines) if m else False,
                     "secs": c["ocr_s"] + c.get("vlm_s", 0)})
        print(f"[{mid} {i + 1}/{len(items)}] {x['file']} truth={rows[-1]['truth']} ocr={o} model={m}", flush=True)
    res = {"set": f"real_intl/{meta['dataset']}", "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "dry_run": bool(a.limit) or a.dry_run, "model": mid, "device": reader.device if reader else "CPU (OCR only)",
           "truth": meta["truth_note"], "source": meta["source"],
           "scope": ("Total only. Taiwanese rules (UBN checksum, invoice period, duplicates, Art. 19) do not apply to these "
                     "receipts and are not run. Ledger Room's own reader is not run either (Taiwanese prompt and labels; its "
                     "amount parser reads '60.000' as 60). The OCR + model column is this script's version of Ledger Room's "
                     "idea: accept a total only when two printed sources agree, otherwise send the receipt to a person."),
           "receipts": len(rows), "splits": dict(Counter(x["split"] for x in items)),
           "conditions": {"ocr_only": score_intl(rows, "ocr", None)}}
    if reader is not None:
        res["conditions"]["model_only"] = score_intl(rows, "model", None)
        res["conditions"]["ocr_plus_model"] = {**score_intl([{**r, "agreed": None if r["held"] else r["ocr"]} for r in rows],
                                                            "agreed", "held"),
                                               "note": "a held receipt counts as not read (a person fills the total in)"}
        res["model_answer_printed_on_page"] = sum(r["model_on_page"] for r in rows)
    secs = [r["secs"] for r in rows if r["secs"]]
    res["sec_per_receipt"] = round(sum(secs) / len(secs), 2) if secs else None
    if a.show_reads:
        res["reads"] = [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in r.items()} for r in rows]
    return res


# ---------------------------------------------------------------- dataset preparation (reads downloaded files only)

def prepare_cord(src: Path, dst: Path) -> dict:
    import pyarrow.parquet as pq   # only this step needs pyarrow
    from io import BytesIO
    from PIL import Image
    out = []
    for split in ("validation", "test"):
        files = sorted((src / "data").glob(f"{split}-*.parquet"))
        if not files:
            raise SystemExit(f"no {split} parquet under {src / 'data'}")
        (dst / split).mkdir(parents=True, exist_ok=True)
        k = 0
        for fp in files:
            t = pq.read_table(fp, columns=["image", "ground_truth"])
            for img, gt in zip(t.column("image").to_pylist(), t.column("ground_truth").to_pylist()):
                g = json.loads(gt).get("gt_parse") or {}
                tot = g.get("total")
                tp = tot.get("total_price") if isinstance(tot, dict) else None
                if isinstance(tp, list):   # a few receipts list several values; keep one only when they agree
                    tp = tp[0] if len({to_amount(v) for v in tp}) == 1 else None
                name = f"{split}/{split}_{k:03d}.jpg"
                Image.open(BytesIO(img["bytes"])).convert("RGB").save(dst / name, "JPEG", quality=95)
                out.append({"file": name, "split": split, "total_raw": tp, "total": str(to_amount(tp)) if to_amount(tp) else None})
                k += 1
    meta = {"dataset": "cord_v2", "source": "naver-clova-ix/cord-v2 (Hugging Face), CC BY 4.0; validation + test splits",
            "truth_note": "total = gt_parse.total.total_price from the dataset's own ground truth",
            "receipts": out}
    (dst / "truth.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"written": len(out), "with_total": sum(1 for x in out if x["total"])}


def prepare_zenodo(src: Path, dst: Path) -> dict:
    """Zenodo 13688441 has full-text transcriptions, not field labels. The truth here is the amount on the one line that
    starts with TOTAL in the human transcription; receipts with no such line, or several different amounts, are skipped."""
    from PIL import Image
    dst.mkdir(parents=True, exist_ok=True)
    out = []
    for img in sorted(src.glob("*.jpg")):
        lab = src / "Labels" / f"{img.stem}.txt"
        if not lab.exists():
            continue
        amts = set()
        for line in lab.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*(?:grand\s+)?total\b(?!\s*(?:tax|items?|qty))[^\d$]*\$?\s*([\d,]+\.\d{2})\b", line, re.I)
            if m:
                amts.add(to_amount(m.group(1)))
        name = img.name
        Image.open(img).convert("RGB").save(dst / name, "JPEG", quality=95)
        out.append({"file": name, "split": "all", "total": str(next(iter(amts))) if len(amts) == 1 else None})
    meta = {"dataset": "zenodo_13688441", "source": "Zenodo record 13688441 (Express Expense SRD extension), CC BY 4.0",
            "truth_note": "total = the amount on the line starting with TOTAL in the human transcription (not a field label)",
            "receipts": out}
    (dst / "truth.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"written": len(out), "with_total": sum(1 for x in out if x["total"])}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("tw", "intl"):
        s = sub.add_parser(name)
        s.add_argument("--model-dir")
        s.add_argument("--device", default=None)
        s.add_argument("--ocr-only", action="store_true")
        s.add_argument("--limit", type=int, default=0)
        s.add_argument("--dry-run", action="store_true", help="mark the output as a dry run")
        s.add_argument("--no-cache", action="store_true")
        s.add_argument("--show-reads", action="store_true", help="include per-photo values in the output")
        s.add_argument("--out")
        if name == "tw":
            s.add_argument("--official", action="store_true", help="first full model run on the set (B70)")
            s.add_argument("--set", default=TW_SET, choices=TW_SETS, help="real_tw (default) or real_tw2")
        else:
            s.add_argument("--root", required=True, help="prepared folder with truth.json (on F:)")
            s.add_argument("--split", help="e.g. validation,test")
    for name in ("prepare-cord", "prepare-zenodo"):
        s = sub.add_parser(name)
        s.add_argument("--src", required=True)
        s.add_argument("--dst", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "prepare-cord":
        print(json.dumps(prepare_cord(Path(a.src), Path(a.dst))))
        return
    if a.cmd == "prepare-zenodo":
        print(json.dumps(prepare_zenodo(Path(a.src), Path(a.dst))))
        return
    if not a.ocr_only and not a.model_dir:
        raise SystemExit("--model-dir or --ocr-only")
    res = run_tw(a) if a.cmd == "tw" else run_intl(a)
    key = res.get("model", "ocr-only") if a.cmd == "tw" else res["model"]
    if a.out:
        out = Path(a.out)
    else:
        tag = res["set"].replace("/", "_")
        out = OUT_DIR / f"{tag}_{key}{'_dryrun' if res['dry_run'] else ''}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    allr = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    if allr.get(key, {}).get("first_run"):
        raise SystemExit(f"{out} already holds the first full run for {key}; not overwriting it")
    allr[key] = res
    out.write_text(json.dumps(allr, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "reads"}, ensure_ascii=False, indent=1, default=str))
    print(f"-> {out}")


if __name__ == "__main__":
    main()
