"""Field boxes drawn on the voucher image: around the value only, turned with the paper.

The reader keeps one box per field: the axis-aligned box of the whole OCR line the value was found on (ocr.read_lines
squeezes OCR's four corner points to min/max). On a tilted voucher that box is much larger than the text, and on a
labelled line it also frames the label ("字軌號碼", "統一編號：").

tighten() reads that line box again with OCR word boxes (RapidOCR return_word_box: one box per CJK character and per
Latin/digit run, each a four-point polygon in the turned line), finds the value's characters in the line, and returns
the smallest turned rectangle around them ("chars"). When OCR gives no word boxes it cuts the line polygon at the
value's position in the line text, weighting CJK characters double ("ratio"). When the value is not in the text it
returns the line polygon itself ("line"), and the reader's own box when OCR finds nothing ("box").

Nothing here changes what is read or decided: it is drawing only. The web app asks for it per document and keeps the
answers in runs/<run>/field_boxes.json; `python -m ledger_room.tight_boxes RUN` fills that file for a whole run.
"""
from __future__ import annotations

import json
import math
import re
import threading
from datetime import date
from pathlib import Path

import numpy as np

VERSION = "t3"
FIELDS = ("invoice_no", "date", "seller_ubn", "buyer_ubn", "net", "tax", "total", "invoice_period",
          "seller_name", "buyer_name", "items", "note")
PAD = 4                  # px around the value, along and across the line
LOOSE = ("items",)       # an item line is framed whole: the reader often keeps only the name, the paper has more
_file_lock = threading.Lock()


# ---------------- finding the value's characters in a line ----------------

def _wide(ch: str) -> bool:
    return ord(ch) > 0x2E80


def _chars(text: str, quad, words, contents) -> list[tuple[str, list]]:
    """[(char, four points)] along the line: word boxes split evenly over their characters, or, without word boxes,
    the line polygon split by character width (CJK = 2, other = 1)."""
    out: list[tuple[str, list]] = []
    if words and contents and len(words) == len(contents):
        for wq, wc in zip(words, contents):
            wc = str(wc)
            n = len(wc)
            for i, ch in enumerate(wc):
                out.append((ch, _sub(wq, i / n, (i + 1) / n)))
        return out
    t = str(text)
    w = [2 if _wide(c) else 1 for c in t]
    tot = sum(w) or 1
    acc = 0
    for c, k in zip(t, w):
        out.append((c, _sub(quad, acc / tot, (acc + k) / tot)))
        acc += k
    return out


def _sub(q, a: float, b: float) -> list:
    """The part of quad q (TL, TR, BR, BL) between fractions a and b of its length."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = [tuple(map(float, p)) for p in q]
    lerp = lambda p, r, f: (p[0] + (r[0] - p[0]) * f, p[1] + (r[1] - p[1]) * f)  # noqa: E731
    return [lerp((x0, y0), (x1, y1), a), lerp((x0, y0), (x1, y1), b), lerp((x3, y3), (x2, y2), b), lerp((x3, y3), (x2, y2), a)]


def _num_spans(s: str):
    for m in re.finditer(r"\d[\d,，.]*\d|\d", s):
        tok = m.group(0)
        yield m.start(), m.end(), tok


def _date_ok(y: int, m: int, d: int, iso: str) -> bool:
    if y < 1000:
        y += 1911
    try:
        return date(y, m, d).isoformat() == iso
    except ValueError:
        return False


def find_span(field: str, value, s: str) -> tuple[int, int] | None:
    """[start, end) of the value in line text s, or None."""
    if value in (None, "", []):
        return None
    if field in ("net", "tax", "total"):
        for a, b, tok in _num_spans(s):
            digits = re.sub(r"\D", "", tok.split(".")[0] if "." in tok and "," not in tok else tok)
            if digits and int(digits) == int(value):
                return a, b
        return None
    if field in ("invoice_no", "seller_ubn", "buyer_ubn"):
        want = re.sub(r"[^A-Z0-9]", "", str(value).upper())
        keep = [(i, c.upper()) for i, c in enumerate(s) if c.isascii() and c.isalnum()]
        norm = "".join(c for _, c in keep)
        k = norm.find(want)
        if k < 0 and field == "invoice_no" and len(want) == 10:
            k = norm.find(want[2:])         # letters misread by OCR: frame the digits and the two characters before
            if k >= 0:
                k = max(0, k - 2)
                want = norm[k:k + 10]
        if k < 0:
            return None
        return keep[k][0], keep[k + len(want) - 1][0] + 1
    if field == "date":
        iso = str(value)[:10]
        pat = r"(\d{2,4})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*日?"
        for m in re.finditer(pat, s):
            if _date_ok(*map(int, m.groups()), iso):
                return m.start(), m.end()
        for m in re.finditer(r"\d{7,8}", s):
            t = m.group(0)
            y, mo, d = (int(t[:3]), int(t[3:5]), int(t[5:])) if len(t) == 7 else (int(t[:4]), int(t[4:6]), int(t[6:]))
            if _date_ok(y, mo, d, iso):
                return m.start(), m.end()
        return None
    # text fields: the value's characters in order, else nothing
    v = re.sub(r"\s", "", str(value if field != "items" else (value[0].get("desc") if value else "")))
    if v and v in s:
        k = s.find(v)
        return k, k + len(v)
    return None


def _after_label(field: str, s: str) -> tuple[int, int] | None:
    """When the value itself is not found: the part after a label (a colon, or the run of digits on a number line)."""
    if field in ("invoice_no", "seller_ubn", "buyer_ubn", "net", "tax", "total", "date", "invoice_period"):
        ds = [i for i, c in enumerate(s) if c.isascii() and c.isalnum()]
        if not ds:
            return None
        end = ds[-1] + 1
        if field == "date" and "日" in s[end:end + 2]:
            end = s.index("日", end) + 1
        if field == "invoice_period" and "月" in s[end:end + 2]:
            end = s.index("月", end) + 1
        return ds[0], end
    m = re.search(r"[:：]\s*", s)
    if m and m.end() < len(s):
        return m.end(), len(s)
    return None


# ---------------- geometry ----------------

def _angle(quads) -> float | None:
    """Direction of the text in degrees: the median of the characters' top edges (each character polygon is cut from
    its OCR line, so it carries that line's turn). A cloud of points from a few short, slightly offset pieces of one
    row would otherwise give a minimum-area rectangle turned against the print."""
    a = []
    for q in quads:
        dx, dy = q[1][0] - q[0][0], q[1][1] - q[0][1]
        if abs(dx) + abs(dy) > 1e-6:
            a.append(math.degrees(math.atan2(dy, dx)))
    return float(np.median(a)) if a else None


def _rect(points, pad: float = PAD, angle: float | None = None) -> list[list[float]]:
    """Smallest rectangle around the points turned to angle (degrees; None: the minimum-area turn), grown by pad,
    as TL, TR, BR, BL (TL = upper left in the paper)."""
    if angle is None:
        import cv2
        (_, _), (w, h), ang = cv2.minAreaRect(np.asarray(points, dtype=np.float32))
        if w < h:                         # keep the long side along the text
            ang += 90
        if ang > 45:
            ang -= 180
        elif ang < -45:
            ang += 180
    else:
        ang = angle
    a = math.radians(ang)
    ux, uy = math.cos(a), math.sin(a)          # along the line
    vx, vy = -uy, ux                           # across, downwards
    us = [p[0] * ux + p[1] * uy for p in points]
    vs = [p[0] * vx + p[1] * vy for p in points]
    mu, mv = (min(us) + max(us)) / 2, (min(vs) + max(vs)) / 2
    cx, cy = mu * ux + mv * vx, mu * uy + mv * vy
    hw, hh = (max(us) - min(us)) / 2 + pad, (max(vs) - min(vs)) / 2 + pad
    return [[round(cx - ux * hw - vx * hh, 1), round(cy - uy * hw - vy * hh, 1)],
            [round(cx + ux * hw - vx * hh, 1), round(cy + uy * hw - vy * hh, 1)],
            [round(cx + ux * hw + vx * hh, 1), round(cy + uy * hw + vy * hh, 1)],
            [round(cx - ux * hw + vx * hh, 1), round(cy - uy * hw + vy * hh, 1)]]


def _bbox(q) -> list[int]:
    xs, ys = [p[0] for p in q], [p[1] for p in q]
    return [int(math.floor(min(xs))), int(math.floor(min(ys))), int(math.ceil(max(xs))), int(math.ceil(max(ys)))]


def _area(q) -> float:
    return 0.5 * abs(sum(q[i][0] * q[(i + 1) % 4][1] - q[(i + 1) % 4][0] * q[i][1] for i in range(4)))


# ---------------- OCR on the line box ----------------

def _ocr_words(rgb: np.ndarray):
    """RapidOCR on an RGB crop with word boxes: [(line quad, text, word quads, word texts)]."""
    from . import ocr
    eng = ocr._get()
    op = eng.text_det.postprocess_op
    bgr = np.ascontiguousarray(rgb[:, :, ::-1])
    with ocr._lock:
        res, _ = eng(bgr, return_word_box=True, box_thresh=op.box_thresh, unclip_ratio=op.unclip_ratio,
                     text_score=eng.text_score)
    out = []
    for r in res or []:
        quad, text = r[0], r[1]
        words = r[3] if len(r) > 3 else []
        contents = r[4] if len(r) > 4 else []
        out.append((quad, str(text), words or [], contents or []))
    return out


def _rows(lines) -> list[tuple[list, str]]:
    """OCR lines on the same printed row joined left to right (a widely spaced date such as "115年 09月 21 日" comes
    back as several lines): [(chars with their polygons, "chars" | "ratio")]."""
    items = []
    for quad, text, words, contents in lines:
        q = [[float(a), float(b)] for a, b in quad]
        cy = sum(p[1] for p in q) / 4
        h = max(1.0, (math.dist(q[0], q[3]) + math.dist(q[1], q[2])) / 2)
        cx = sum(p[0] for p in q) / 4
        how = "chars" if words and len(words) == len(contents) else "ratio"
        items.append({"cy": cy, "h": h, "cx": cx, "chars": _chars(text, q, words, contents), "how": how})
    rows: list[list[dict]] = []
    for it in sorted(items, key=lambda i: i["cy"]):
        for r in rows:
            ref = r[-1]
            if abs(it["cy"] - ref["cy"]) < 0.5 * min(it["h"], ref["h"]):
                r.append(it)
                break
        else:
            rows.append([it])
    out = []
    for r in rows:
        r.sort(key=lambda i: i["cx"])
        out.append(([c for i in r for c in i["chars"]], "chars" if all(i["how"] == "chars" for i in r) else "ratio"))
    return out


def tighten(img, box, field: str, value) -> dict:
    """{"quad": four points, "box": its axis-aligned box, "how": chars|ratio|line|box} in image pixels.
    img: a PIL image (RGB). box: the reader's box [x0, y0, x1, y1] for this field."""
    x0, y0, x1, y1 = [int(v) for v in box]
    W, H = img.size
    h = max(1, y1 - y0)
    px, py = 8, max(6, int(h * 0.25))
    cx0, cy0, cx1, cy1 = max(0, x0 - px), max(0, y0 - py), min(W, x1 + px), min(H, y1 + py)
    fallback = {"quad": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "box": [x0, y0, x1, y1], "how": "box"}
    if cx1 - cx0 < 4 or cy1 - cy0 < 4:
        return fallback
    crop = np.asarray(img.crop((cx0, cy0, cx1, cy1)).convert("RGB"))
    s = 1.0
    if crop.shape[0] < 64:                       # small text: enlarge so the recogniser has enough pixels
        s = 64 / crop.shape[0]
        import cv2
        crop = cv2.resize(crop, (max(1, int(crop.shape[1] * s)), 64), interpolation=cv2.INTER_CUBIC)
    B = 24
    canvas = np.full((crop.shape[0] + 2 * B, crop.shape[1] + 2 * B, 3), 255, dtype=np.uint8)
    canvas[B:B + crop.shape[0], B:B + crop.shape[1]] = crop
    try:
        lines = _ocr_words(canvas)
    except Exception:
        return fallback
    back = lambda p: [cx0 + (float(p[0]) - B) / s, cy0 + (float(p[1]) - B) / s]  # noqa: E731
    best = None
    for chars, how in _rows(lines):
        joined = "".join(c for c, _ in chars)
        sp = find_span(field, value, joined)
        rank = 0
        if sp is None:
            sp, how, rank = _after_label(field, joined), how, 1
        if sp is None:
            continue
        sel = [[back(p) for p in q] for _, q in chars[sp[0]:sp[1]]]
        pts = [p for q in sel for p in q]
        if not pts:
            continue
        ys = [p[1] for p in pts]
        if rank and not y0 <= (min(ys) + max(ys)) / 2 <= y1:
            continue                 # a guess after a label only on the row the reader drew, not the next one
        cand = (rank, -len(pts), pts, how, _angle(sel))
        if best is None or cand[:2] < best[:2]:
            best = cand
    if best is None:
        # the value is not readable in the crop: the turned polygon of the row nearest the middle of the reader's
        # box (the crop's margin can reach the next printed row), else the reader's box
        rows = []
        mid = (y0 + y1) / 2
        for chars, _ in _rows(lines):
            sel = [[back(p) for p in cq] for _, cq in chars]
            ys = [p[1] for q in sel for p in q]
            if sel and y0 <= (min(ys) + max(ys)) / 2 <= y1:
                rows.append((abs((min(ys) + max(ys)) / 2 - mid), sel))
        if not rows:
            return fallback
        sel = min(rows, key=lambda r: r[0])[1]
        q = _rect([p for cq in sel for p in cq], pad=1, angle=_angle(sel))
        return {"quad": q, "box": _bbox(q), "how": "line"}
    q = _rect(best[2], pad=PAD, angle=best[4])
    # never larger than the reader's own box (a stray match could spread across the crop)
    if _area(q) > (x1 - x0 + 2 * PAD) * (y1 - y0 + 2 * PAD) * 1.05:
        return fallback
    return {"quad": q, "box": _bbox(q), "how": best[3]}


def _value(doc: dict, f: str):
    v = (doc.get("fields") or {}).get(f)
    return v


def _sig(doc: dict, f: str, box) -> str:
    v = _value(doc, f)
    if f == "items":
        v = (v or [{}])[0].get("desc") if v else None
    return f"{VERSION}|{v}|{list(box)}"


def relocate(img_path: Path, doc: dict, f: str) -> list[int] | None:
    """The line box of a value a person corrected, found again on the page (the reader's box points at the misread)."""
    from . import ocr
    from .reader import locate_amount, locate_text
    v = _value(doc, f)
    if v in (None, ""):
        return None
    lines = ocr.read_lines(Path(img_path))
    if f in ("net", "tax", "total"):
        ln = locate_amount(lines, int(v))
    elif f == "date":
        ln = next((l for l in lines if find_span("date", v, l.text)), None)
    else:
        ln = next((l for l in lines if find_span(f, v, l.text)), None) or locate_text(lines, str(v), 0.99)
    return list(ln.box) if ln is not None else None


def for_doc(img_path: Path, doc: dict, cache: dict | None = None) -> dict[str, dict]:
    """Tight boxes for every boxed field of one document (result.json "docs" entry, corrections applied)."""
    from PIL import Image
    cache = cache if cache is not None else {}
    out: dict[str, dict] = {}
    img = None
    boxes = dict(doc.get("boxes") or {})
    corrected = set((doc.get("corrections") or {}))
    for f in FIELDS:
        box = boxes.get(f)
        if f in corrected:
            key = f"{VERSION}|moved|{_value(doc, f)}|{doc.get('doc_id')}"
            if key in cache:
                out[f] = cache[key]
                continue
            nb = None
            try:
                nb = relocate(img_path, doc, f)
            except Exception:
                nb = None
            if nb is None:
                out[f] = cache[key] = {"none": True}     # the corrected value is not printed where OCR can find it
                continue
            box = nb
            if img is None:
                img = Image.open(img_path).convert("RGB")
            out[f] = cache[key] = tighten(img, box, f, _value(doc, f)) | {"moved": True}
            continue
        if not box or len(box) != 4:
            continue
        if f in LOOSE:
            out[f] = {"quad": [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]],
                      "box": list(box), "how": "box"}
            continue
        key = _sig(doc, f, box)
        if key in cache:
            out[f] = cache[key]
            continue
        if img is None:
            img = Image.open(img_path).convert("RGB")
        v = _value(doc, f)
        out[f] = cache[key] = tighten(img, box, f, v)
    return out


def load_cache(run_dir: Path) -> dict:
    p = run_dir / "field_boxes.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if d.get("version") == VERSION else {"version": VERSION, "docs": {}}
    except (OSError, ValueError):
        return {"version": VERSION, "docs": {}}


def save_cache(run_dir: Path, data: dict) -> None:
    p = run_dir / "field_boxes.json"
    with _file_lock:
        tmp = p.with_name(f"field_boxes.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)


def main(argv=None):
    import argparse
    from . import pipeline
    ap = argparse.ArgumentParser(description="fill runs/<run>/field_boxes.json for every document of a run")
    ap.add_argument("run")
    a = ap.parse_args(argv)
    rdir = pipeline.OUT / "runs" / a.run
    res = json.loads((rdir / "result.json").read_text(encoding="utf-8"))
    data = load_cache(rdir)
    n = 0
    for d in res["docs"]:
        if not d.get("image"):
            continue
        cache = data["docs"].setdefault(d["doc_id"], {})
        r = for_doc(pipeline.DATA / d["image"], d, cache)
        n += len(r)
    save_cache(rdir, data)
    print(f"{a.run}: {n} field boxes")


if __name__ == "__main__":
    main()
