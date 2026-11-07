"""Split one scanned page that holds several vouchers into one box per voucher (classical CV, CPU only).

A firm scans A4 pages with several vouchers taped on each (e-invoice strips side by side, utility bills two by two,
postage receipts turned sideways). split_page() returns one Box per voucher in page pixels; nothing here reads text.
Not connected to the pipeline yet: call it yourself, or run `python -m ledger_room.split page.jpg [out_dir]`.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import cv2
import numpy as np

WORK_LONG = 780          # pages are analysed at this long side (a 200 dpi A4 page is 2337 px long, so about 1/3)
REF_LONG = 2337.0        # sizes below are in pixels of a 200 dpi A4 page and scaled to the page given
INK_LEVEL = 200          # grey level below which a page pixel is ink
P0 = {                   # tuned on the dev pages only (eval/split_eval.py); sizes in reference pixels
    "close": 60,         # closing that turns a voucher's print into one blob
    "min_blob": 250,     # blobs smaller than this both ways are notes or specks
    "min_w": 300,        # a cut must leave at least this much on each side (narrowest voucher about 310 wide)
    "min_h": 330,
    "gap_v": 9,          # near-empty band at least this wide: vertical cut / horizontal cut
    "gap_h": 45,
    "max_gap_h": 100,    # a horizontal blank band wider than this is the blank middle of one bill, not a gap
    "empty_v": 0.02,     # near-empty: at most this share of the band is ink
    "empty_h": 0.01,
    "smooth": 3,         # work pixels the column / row ink share is averaged over
    "line_len": 250,     # thin straight runs at least this long are rules or paper edges, not print
    "seg_len": 45,       # thin straight pieces at least this long are ignored along a cut (a broken paper edge)
    "line_thick": 9,     # thin: at most this thick
    "min_side": 250,     # boxes smaller than this either way are dropped
    "pad": 15,           # boxes are grown by this much (print stops short of the paper edge)
}


@dataclass(frozen=True)
class Box:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0

    def as_list(self) -> list[int]:
        return [self.x0, self.y0, self.x1, self.y1]


def _rgb(image) -> np.ndarray:
    if isinstance(image, (str, os.PathLike)):
        a = cv2.imdecode(np.fromfile(str(image), np.uint8), cv2.IMREAD_COLOR)
        if a is None:
            raise ValueError(f"cannot read image {image}")
        return cv2.cvtColor(a, cv2.COLOR_BGR2RGB)
    a = np.asarray(image)
    if a.ndim == 2:
        a = np.repeat(a[:, :, None], 3, axis=2)
    return np.ascontiguousarray(a[:, :, :3]).astype(np.uint8)


def _ink(gray: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Dark marks found at full resolution (thin photocopied strokes vanish when shrunk first), then shrunk:
    a work pixel is ink when any of the page pixels under it is."""
    dark = (gray < INK_LEVEL).astype(np.float32)
    return (cv2.resize(dark, size, interpolation=cv2.INTER_AREA) > 0.02).astype(np.uint8)


def _runs(mask1d: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) runs of True."""
    d = np.diff(np.concatenate([[0], mask1d.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def _color(rgb_small: np.ndarray) -> np.ndarray:
    """Pen and stamp colour (blue / red ink): strongly saturated pixels. Handwritten notes on the page are in pen."""
    hsv = cv2.cvtColor(rgb_small, cv2.COLOR_RGB2HSV)
    return ((hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 60)).astype(np.uint8)


def _long_lines(ink: np.ndarray, length: int, thick: int) -> tuple[np.ndarray, np.ndarray]:
    """Thin horizontal and vertical straight runs (table rules, box borders, paper edges). Solid areas (QR codes,
    barcodes, grey bands) are not lines: a pixel inside a run of `thick` across the line is dropped."""
    rect = lambda w, h: cv2.getStructuringElement(cv2.MORPH_RECT, (w, h))   # noqa: E731
    h = cv2.morphologyEx(ink, cv2.MORPH_OPEN, rect(length, 1))
    v = cv2.morphologyEx(ink, cv2.MORPH_OPEN, rect(1, length))
    h &= 1 - cv2.morphologyEx(ink, cv2.MORPH_OPEN, rect(1, thick))
    v &= 1 - cv2.morphologyEx(ink, cv2.MORPH_OPEN, rect(thick, 1))
    return h, v


def _tight(t: np.ndarray, b):
    x0, y0, x1, y1 = b
    sub = t[y0:y1, x0:x1]
    if not sub.any():
        return None
    ys = np.flatnonzero(sub.any(axis=1))
    xs = np.flatnonzero(sub.any(axis=0))
    return [x0 + xs[0], y0 + ys[0], x0 + xs[-1] + 1, y0 + ys[-1] + 1]


def _split(t: np.ndarray, hl: np.ndarray, vl: np.ndarray, hs: np.ndarray, vs: np.ndarray, b, P: dict,
           out: list) -> None:
    """Cut a region along the widest near-empty band across it that leaves both sides voucher sized; recurse.
    Lines across the band block it (a table or box inside one voucher); lines along it do not (a paper edge)."""
    b = _tight(t, b)
    if b is None:
        return
    x0, y0, x1, y1 = b
    best = None
    for axis, lo, gap, top, empty, cross, along in (
            (0, P["min_w"], P["gap_v"], 1 << 30, P["empty_v"], hl, vs),       # axis 0: a vertical cut
            (1, P["min_h"], P["gap_h"], P["max_gap_h"], P["empty_h"], vl, hs)):
        sub = ((t[y0:y1, x0:x1] & (1 - along[y0:y1, x0:x1])) | cross[y0:y1, x0:x1]).astype(np.float32)
        prof = sub.mean(axis=axis)
        if P["smooth"] > 1:
            prof = np.convolve(prof, np.ones(P["smooth"]) / P["smooth"], mode="same")
        n = len(prof)
        for a, e in _runs(prof <= empty):
            c = (a + e) // 2
            if gap <= e - a <= top and c >= lo and n - c >= lo and (best is None or e - a > best[0]):
                best = (e - a, axis, c)
    if best is None:
        out.append(b)
        return
    _, axis, c = best
    if P.get("log") is not None:
        P["log"].append((axis, [x0, y0, x1, y1], c, best[0]))
    halves = ([x0, y0, x0 + c, y1], [x0 + c, y0, x1, y1]) if axis == 0 else ([x0, y0, x1, y0 + c], [x0, y0 + c, x1, y1])
    for h in halves:
        _split(t, hl, vl, hs, vs, h, P, out)


def _merge_overlapping(boxes: list) -> list:
    boxes = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    changed = True
                    break
            if changed:
                break
    return boxes


def split_page(image, _debug: dict | None = None) -> list[Box]:
    """image: path, PIL image or numpy array (RGB or grey). Returns voucher boxes in page pixels, reading order."""
    rgb = _rgb(image)
    H, W = rgb.shape[:2]
    s = WORK_LONG / max(H, W)
    small_rgb = cv2.resize(rgb, (max(1, round(W * s)), max(1, round(H * s))), interpolation=cv2.INTER_AREA)
    small = cv2.cvtColor(small_rgb, cv2.COLOR_RGB2GRAY)
    u = max(H, W) / REF_LONG * s             # work pixels per reference pixel
    r = lambda v: max(1, int(round(v * u)))   # noqa: E731
    ink = _ink(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), small.shape[::-1])
    ink[_color(small_rgb) > 0] = 0
    hl, vl = _long_lines(ink, r(P0["line_len"]), r(P0["line_thick"]))
    hs, vs = _long_lines(ink, r(P0["seg_len"]), r(P0["line_thick"]))   # shorter pieces: broken paper edges
    text = ink & (1 - hl) & (1 - vl)
    # regions: text and lines closed into solid blobs; overlapping blobs joined; small isolated ones (notes) dropped
    k = r(P0["close"])
    blob = cv2.morphologyEx(text | hl | vl, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    n, lab, st, _ = cv2.connectedComponentsWithStats(blob, 8)
    regions = _merge_overlapping([[x, y, x + w, y + h] for x, y, w, h, _ in st[1:]])
    regions = [b for b in regions if b[2] - b[0] >= r(P0["min_blob"]) or b[3] - b[1] >= r(P0["min_blob"])]
    P = {k: r(P0[k]) for k in ("min_w", "min_h", "gap_v", "gap_h", "max_gap_h")}
    P.update(empty_v=P0["empty_v"], empty_h=P0["empty_h"], smooth=P0["smooth"], log=[] if _debug is not None else None)
    leaves: list = []
    for b in regions:
        _split(text, hl, vl, hs, vs, b, P, leaves)
    if _debug is not None:
        _debug.update(cuts=P["log"], ink=ink, text=text, blob=blob, lines=hl | vl, leaves=leaves, scale=s)
    min_side = P0["min_side"] * u
    boxes = [b for b in leaves if (b[2] - b[0]) >= min_side and (b[3] - b[1]) >= min_side]
    pad = P0["pad"] * max(H, W) / REF_LONG
    out = [Box(max(0, int(b[0] / s - pad)), max(0, int(b[1] / s - pad)), min(W, int(np.ceil(b[2] / s + pad))),
               min(H, int(np.ceil(b[3] / s + pad)))) for b in boxes]
    return sorted(out, key=lambda b: (b.y0 // max(1, H // 3), b.x0))


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m ledger_room.split page.jpg [out_dir]   (prints boxes; saves crops into out_dir)")
        return 2
    boxes = split_page(argv[0])
    for i, b in enumerate(boxes, 1):
        print(i, b.as_list())
    if len(argv) > 1:
        os.makedirs(argv[1], exist_ok=True)
        img = cv2.imdecode(np.fromfile(argv[0], np.uint8), cv2.IMREAD_COLOR)
        stem = os.path.splitext(os.path.basename(argv[0]))[0]
        for i, b in enumerate(boxes, 1):
            ok, buf = cv2.imencode(".jpg", img[b.y0:b.y1, b.x0:b.x1])
            buf.tofile(os.path.join(argv[1], f"{stem}_v{i:02d}.jpg"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
