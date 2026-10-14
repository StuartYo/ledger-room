"""OCR-only control: the same OCR, the same reconciliation code and the same rules, with no vision-language model.

python -m eval.ocr_only                     # held-out set -> eval/results_ocr_only.json (merged by eval.run_eval)
python -m eval.real_check --ocr-only ...    # the owner's real photos with this reader (see real_check.py)

What the model normally supplies is replaced by plain OCR parsing:
  * every field the reconciliation code already takes from OCR (document type keywords, invoice number, dates,
    UBN roles from the words around each number, the total row, item rows, the stamp's seller name) is unchanged;
  * net and tax come from the number on the 銷售額 / 稅額 (營業稅) row; the buyer name from the 買受人 / 客戶 line;
  * a buyer UBN printed in boxes that the page OCR misses is re-read once from the enlarged crop (one digit per box).
    Ledger Room accepts such a read only when two of three independent reads agree, two of which come from the model;
    with no model there is nothing to vote with, so the single OCR re-read is accepted as is ("ocr-zoom-single").
  * no category from a model: categories come only from the keyword tables in rules.py.
The question this answers: what does the vision model actually add on top of OCR plus code?
"""
from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PIL import Image

from ledger_room import normalize as N
from ledger_room.ocr import Line, read_lines
from ledger_room.reader import buyer_label_line, postprocess, read_boxed_digits, _INV

ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "ocr-only"
NET_LABELS = ("銷售額", "未稅")
TAX_LABELS = ("稅額", "營業稅")
# OCR mixes simplified and traditional characters (銷售额, 營業税): labels are matched after folding these
FOLD = str.maketrans({"额": "額", "税": "稅", "营": "營", "业": "業", "销": "銷", "买": "買", "户": "戶"})
BUYER_LABELS = ("買受人", "客戶")
_NUM = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d{1,7})(?![\d.])")


def _row_numbers(lines: list[Line], lab: Line) -> list[int]:
    """Numbers printed after the label on the same OCR line, or on lines to its right in the same row."""
    h = max(12, lab.box[3] - lab.box[1])
    cy = (lab.box[1] + lab.box[3]) / 2
    out = []
    for l in [lab] + sorted((l for l in lines if l is not lab and l.box[0] > lab.box[0]
                             and abs((l.box[1] + l.box[3]) / 2 - cy) < 0.8 * h), key=lambda l: l.box[0]):
        t = _INV.sub(" ", l.text)
        for m in _NUM.finditer(t):
            v = N.to_int(m.group(1))
            if v is not None:
                out.append(v)
    return out


def row_amount(lines: list[Line], labels: tuple[str, ...], exclude: tuple[str, ...] = ()) -> int | None:
    for lab in sorted(lines, key=lambda l: l.box[1]):
        t = lab.text.translate(FOLD)
        if any(k in t for k in labels) and not any(k in t for k in exclude):
            nums = _row_numbers(lines, lab)
            if nums:
                return nums[-1]   # the amount column is the rightmost number on the row
    return None


def buyer_name_from_ocr(lines: list[Line]) -> str | None:
    for l in lines:
        t = l.text.translate(FOLD)
        for k in BUYER_LABELS:
            if k in t:
                rest = re.sub(r"^[\s:：]+", "", t.split(k, 1)[1])
                if len(re.sub(r"[^一-鿿]", "", rest)) >= 3:
                    return rest.strip()
    return None


class OcrOnlyReader:
    model_id = MODEL_ID
    device = "CPU (OCR only)"
    thinking = "n/a"
    load_s = 0.0

    def read_raw(self, path: str | Path) -> dict:
        path = Path(path)
        img = Image.open(path).convert("RGB")
        t = time.time()
        lines = read_lines(path)
        return {"ocr_lines": [asdict(l) for l in lines], "raw": "", "timing": {"ocr_s": round(time.time() - t, 2)},
                "image_size": list(img.size), "model": MODEL_ID, "device": self.device}

    def read(self, path: str | Path, raw: dict | None = None) -> dict:
        path = Path(path)
        raw = raw or self.read_raw(path)
        lines = [Line(l["id"], l["text"], tuple(l["box"]), l["conf"]) for l in raw["ocr_lines"]]
        timing = dict(raw["timing"])
        fields, prov, boxes, extra = postprocess({}, lines)
        if prov.get("buyer_ubn") == "needs-zoom":
            t = time.time()
            lab = buyer_label_line(lines)
            v = None
            if lab is not None:
                img = Image.open(path).convert("RGB")
                x0, y0, x1, y1 = lab.box
                h = y1 - y0
                box = [max(0, x1 - 4), max(0, int(y0 - 0.8 * h)), min(img.width, x1 + 520), min(img.height, int(y1 + 0.8 * h))]
                v = read_boxed_digits(img.crop(box))
                if v:
                    boxes["buyer_ubn"] = box
            fields["buyer_ubn"], prov["buyer_ubn"] = v, ("ocr-zoom-single" if v else "unconfirmed")
            timing["zoom_s"] = round(time.time() - t, 2)
        net = row_amount(lines, NET_LABELS)
        tax = row_amount(lines, TAX_LABELS, exclude=NET_LABELS)
        if fields.get("doc_type") in ("e_invoice", "triplicate_invoice", "two_part_register") or tax is not None:
            fields["net"], fields["tax"] = net, tax
            prov["net"] = "ocr-row" if net is not None else "missing"
            prov["tax"] = "ocr-row" if tax is not None else "missing"
        else:   # a plain receipt prints no net or tax line
            fields["net"], fields["tax"] = None, None
        if not fields.get("buyer_name"):
            fields["buyer_name"] = buyer_name_from_ocr(lines)
            prov["buyer_name"] = "ocr" if fields["buyer_name"] else "missing"
        timing["total_s"] = round(sum(timing.get(k, 0) for k in ("ocr_s", "zoom_s")), 2)
        return {"fields": fields, "provenance": prov, "boxes": boxes, "extra": extra, "ocr_lines": raw["ocr_lines"],
                "raw": "", "parsed": True, "timing": timing, "model": MODEL_ID, "device": self.device,
                "image_size": raw["image_size"]}


def main():
    from eval.read_accuracy import score_reads
    from eval.run_eval import load_expected, score, secs
    from ledger_room import pipeline, rules
    from ledger_room.pipeline import git_rev

    out = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "code_rev": git_rev(),
           "reader": "OCR only (PP-OCRv4 on OpenVINO, CPU) + the same reconciliation code and rules; no vision-language model",
           "zoom": "a boxed buyer UBN is re-read once from the enlarged crop and accepted without a vote (no model to vote with)",
           "sets": {}}
    reader = OcrOnlyReader()
    for s in ("heldout", "main"):
        exp = load_expected(s)
        expd = {d["doc_id"]: set(d["issues"]) for d in exp["docs"]}
        run = pipeline.audit(s, reader, run_id=f"eval-{s}-ocr-only", model_id=MODEL_ID,
                             progress=lambda i, n, did, r: print(f"[ocr-only {s} {i}/{n}] {did}", flush=True))
        pred = {}
        for e in run["exceptions"]:
            if e["rule_id"] in rules.ISSUE_TYPES:
                pred.setdefault(e["doc_id"], set()).add(e["rule_id"])
        held = {e["doc_id"] for e in run["exceptions"] if e["rule_id"] in HOLD_RULES or e["severity"] in ("hold", "review")}
        out["sets"][s] = {
            **score(pred, expd), "sec_per_doc": secs([d["timing"].get("total_s") for d in run["docs"]]),
            "reading": score_reads({d["doc_id"]: d for d in run["docs"]}, exp,
                                   held={e["doc_id"] for e in run["exceptions"] if e["rule_id"] == "AMOUNT_MISMATCH"}),
            "routing_flags": {k: sum(1 for e in run["exceptions"] if e["rule_id"] == k) for k in HOLD_RULES},
            "docs_held_for_review": len(held), "n_docs": len(expd),
        }
    p = ROOT / "eval" / "results_ocr_only.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: {"micro": v["micro"], "per_type": {t: (x["tp"], x["fp"]) for t, x in v["per_type"].items()},
                          "err": v["reading"]["field_error_rate"], "held": v["docs_held_for_review"]}
                      for k, v in out["sets"].items()}, ensure_ascii=False, indent=1))


HOLD_RULES = ("AMOUNT_MISMATCH", "BUYER_PRESUMED", "NOT_A_VOUCHER", "READ_REVIEW")

if __name__ == "__main__":
    main()
