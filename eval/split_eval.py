"""Page splitting: score ledger_room.split.split_page against hand-drawn voucher boxes.

The pages are real scans (private; nothing from them is committed). The key is a JSON file whose docs[] each have
page (file name in --pages), bbox [x0, y0, x1, y1] in page pixels, kind and id.

  python -I eval/split_eval.py --pages DIR --truth KEY.json --out OUT_DIR --which dev [--overlays 6]
  python -I eval/split_eval.py --pages DIR --truth KEY.json --out OUT_DIR --which heldout
  python -I eval/split_eval.py --out OUT_DIR --write-results eval/results_split.json

Held-out rule (fixed before any tuning): sort the page file names; the page at index i is held out when i % 3 == 2.
The method was tuned only on the other pages (dev). Per-page detail and overlays go to OUT_DIR only (keep it outside
the repository); --write-results copies the aggregate counts of dev and of the first held-out run into the repo file.

Matching: every predicted box is paired with at most one key box, greedily by IoU (highest first), pairs below IoU 0.5
are not paired. matched@0.7 counts the pairs whose IoU is at least 0.7. A page is fully right when every key box on it
is paired and no predicted box is left over.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:   # run as a script with python -I: the repository is not on sys.path by itself
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from ledger_room.split import split_page  # noqa: E402

HELDOUT_RULE = "sorted page file names; index % 3 == 2 is held out, the rest is dev"


def iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def match(truth: list, pred: list, thr: float = 0.5) -> list[tuple[int, int, float]]:
    """Greedy one-to-one pairing by IoU, highest first. Returns (truth index, pred index, IoU)."""
    pairs = sorted(((iou(t, p), i, j) for i, t in enumerate(truth) for j, p in enumerate(pred)), reverse=True)
    used_t, used_p, out = set(), set(), []
    for v, i, j in pairs:
        if v < thr:
            break
        if i in used_t or j in used_p:
            continue
        used_t.add(i)
        used_p.add(j)
        out.append((i, j, v))
    return out


def page_split(pages: list[str], which: str) -> list[int]:
    if which == "dev":
        return [i for i in range(len(pages)) if i % 3 != 2]
    if which == "heldout":
        return [i for i in range(len(pages)) if i % 3 == 2]
    return list(range(len(pages)))


def overlay(path: Path, truth: list, pred: list, pairs: list, dst: Path) -> None:
    img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    hit_t = {i for i, _, _ in pairs}
    hit_p = {j for _, j, _ in pairs}
    for i, t in enumerate(truth):
        cv2.rectangle(img, (t[0], t[1]), (t[2], t[3]), (0, 170, 0) if i in hit_t else (0, 0, 255), 7)
    for j, p in enumerate(pred):
        cv2.rectangle(img, (p[0] + 6, p[1] + 6), (p[2] - 6, p[3] - 6), (255, 120, 0) if j in hit_p else (255, 0, 255), 4)
    img = cv2.resize(img, None, fx=0.4, fy=0.4, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    buf.tofile(str(dst))


def run(args) -> dict:
    key = json.loads(Path(args.truth).read_text(encoding="utf-8"))
    by_page = defaultdict(list)
    for d in key["docs"]:
        by_page[d["page"]].append(d)
    pages = sorted(os.listdir(args.pages))
    pages = [p for p in pages if p.lower().endswith((".jpg", ".jpeg", ".png"))]
    idx = page_split(pages, args.which)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tot = Counter()
    kind_n, kind_5, kind_7 = Counter(), Counter(), Counter()
    detail, times, n_ov = [], [], 0
    for i in idx:
        name = pages[i]
        docs = by_page.get(name, [])
        truth = [d["bbox"] for d in docs]
        t0 = time.perf_counter()
        boxes = split_page(Path(args.pages) / name)
        times.append(time.perf_counter() - t0)
        pred = [b.as_list() for b in boxes]
        pairs = match(truth, pred)
        n7 = sum(1 for _, _, v in pairs if v >= 0.7)
        hit = {a for a, _, _ in pairs}
        for k, d in enumerate(docs):
            kind_n[d["kind"]] += 1
            if k in hit:
                kind_5[d["kind"]] += 1
                if next(v for a, _, v in pairs if a == k) >= 0.7:
                    kind_7[d["kind"]] += 1
        right = len(pairs) == len(truth) and len(pred) == len(truth)
        tot.update(pages=1, truth=len(truth), pred=len(pred), m5=len(pairs), m7=n7, right=int(right))
        detail.append({"page_index": i, "truth": len(truth), "pred": len(pred), "matched_05": len(pairs),
                       "matched_07": n7, "missed": [docs[k]["id"] for k in range(len(docs)) if k not in hit],
                       "extras": len(pred) - len(pairs), "fully_right": right,
                       "ious": [round(v, 3) for _, _, v in sorted(pairs)], "pred_boxes": pred})
        if not right and (args.overlays < 0 or n_ov < args.overlays):
            n_ov += 1
            overlay(Path(args.pages) / name, truth, pred, pairs, out / f"{args.which}_{i:03d}.jpg")
    summary = {
        "which": args.which, "pages": tot["pages"], "truth_boxes": tot["truth"], "predicted_boxes": tot["pred"],
        "matched_iou_0.5": tot["m5"], "matched_iou_0.7": tot["m7"], "missed_truth_boxes": tot["truth"] - tot["m5"],
        "extra_boxes": tot["pred"] - tot["m5"], "pages_fully_right": tot["right"],
        "recall_0.5": round(tot["m5"] / max(1, tot["truth"]), 3),
        "precision_0.5": round(tot["m5"] / max(1, tot["pred"]), 3),
        "per_kind": {k: {"truth": kind_n[k], "matched_0.5": kind_5[k], "matched_0.7": kind_7[k],
                         "recall_0.5": round(kind_5[k] / kind_n[k], 3)} for k in sorted(kind_n)},
        "seconds_per_page": {"mean": round(float(np.mean(times)), 3), "max": round(float(np.max(times)), 3)},
        "run_at": datetime.now().isoformat(timespec="seconds"),
    }
    (out / f"{args.which}_pages.json").write_text(json.dumps(detail, indent=1), encoding="utf-8")
    sfile = out / f"{args.which}_summary.json"
    if args.which == "heldout" and not (out / "heldout_first_run.json").exists():
        (out / "heldout_first_run.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    sfile.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def write_results(out_dir: Path, dst: Path, commit: str | None) -> None:
    dev = json.loads((out_dir / "dev_summary.json").read_text(encoding="utf-8"))
    first = json.loads((out_dir / "heldout_first_run.json").read_text(encoding="utf-8"))
    res = {
        "what": "ledger_room.split.split_page: one box per voucher on scanned pages that hold several vouchers",
        "data": "107 real scanned A4 pages of one small company's petty-cash vouchers (336 hand-drawn boxes). PRIVATE: "
                "used with the owner's consent for aggregate numbers only; no image, name or value is in this "
                "repository.",
        "heldout_rule": HELDOUT_RULE,
        "tuning": "every threshold was set on the dev pages only; the held-out pages were run once, after the method "
                  "was frozen, and that first run is the number reported",
        "matching": "greedy one-to-one by IoU; a key box counts as found at IoU >= 0.5; matched_iou_0.7 is the "
                    "stricter count; a page is fully right when every key box is found and no box is left over",
        "code": commit,
        "not_done": "boxes only: crops are not turned upright (the key's rotate field is not scored); "
                    "split_page is not called by the pipeline yet",
        "seen_failures_heldout": "looked at only after the first held-out run: touching vouchers with no blank band "
                                 "between them (e-invoice strips side by side, stacked postage receipts); small "
                                 "receipts in a dense grid; sideways bills cut in two along their blank middle; a pen "
                                 "note joined to the voucher next to it",
        "dev": {k: v for k, v in dev.items() if k != "run_at"},
        "heldout_first_run": {k: v for k, v in first.items() if k != "run_at"},
    }
    dst.write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pages")
    ap.add_argument("--truth")
    ap.add_argument("--out", required=True)
    ap.add_argument("--which", choices=["dev", "heldout", "all"], default="dev")
    ap.add_argument("--overlays", type=int, default=0, help="save up to N overlays of pages not fully right (-1 = all)")
    ap.add_argument("--write-results", metavar="FILE")
    ap.add_argument("--commit")
    args = ap.parse_args()
    if args.write_results:
        write_results(Path(args.out), Path(args.write_results), args.commit)
        return 0
    s = run(args)
    print(json.dumps(s, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
