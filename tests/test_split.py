"""Page splitting on drawn pages: white A4 scans with vouchers made of text-like marks."""
import cv2
import numpy as np
import pytest
from PIL import Image

from ledger_room.split import Box, split_page

PAGE_H, PAGE_W = 1653, 2337          # a 200 dpi A4 page, landscape


def voucher(h: int, w: int, seed: int, margin: int = 20, edge: bool = True, frame: bool = False) -> np.ndarray:
    """A paper voucher: rows of word-like dark blocks, a title row, an optional faint paper edge and printed frame."""
    rng = np.random.default_rng(seed)
    v = np.full((h, w, 3), 255, np.uint8)
    y = margin
    while y + 24 < h - margin:
        x = margin + int(rng.integers(0, 20))
        end = w - margin - int(rng.integers(0, w // 3))
        tall = 22 if y == margin else 12
        while x < end:
            ww = int(rng.integers(8, 40))
            cv2.rectangle(v, (x, y), (min(x + ww, end), y + tall), (40, 40, 40), -1)
            x += ww + int(rng.integers(5, 14))
        y += tall + int(rng.integers(14, 26))
    if frame:
        cv2.rectangle(v, (margin // 2, margin // 2), (w - margin // 2, h - margin // 2), (60, 60, 60), 2)
    if edge:
        cv2.rectangle(v, (0, 0), (w - 1, h - 1), (185, 185, 185), 1)
    return v


def page(*items) -> np.ndarray:
    p = np.full((PAGE_H, PAGE_W, 3), 255, np.uint8)
    for img, x, y in items:
        p[y:y + img.shape[0], x:x + img.shape[1]] = img
    return p


def iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def assert_found(boxes: list[Box], truth: list[list[int]], thr: float = 0.7):
    assert len(boxes) == len(truth), [b.as_list() for b in boxes]
    for t in truth:
        best = max(iou(b.as_list(), t) for b in boxes)
        assert best >= thr, (t, [b.as_list() for b in boxes])


def test_blank_page_has_no_vouchers():
    assert split_page(page()) == []


def test_three_separate_vouchers():
    a, b, c = voucher(1200, 400, 1), voucher(1100, 1000, 2), voucher(500, 600, 3)
    p = page((a, 100, 100), (b, 650, 120), (c, 1700, 300))
    assert_found(split_page(p), [[100, 100, 500, 1300], [650, 120, 1650, 1220], [1700, 300, 2300, 800]])


def test_two_touching_strips_are_split_at_the_paper_edge():
    a, b = voucher(1150, 390, 4, margin=12), voucher(1150, 390, 5, margin=12)
    p = page((a, 300, 150), (b, 690, 150))          # b starts where a ends: no white page between them
    assert_found(split_page(p), [[300, 150, 690, 1300], [690, 150, 1080, 1300]])


def test_rotated_voucher_next_to_an_upright_one():
    side = np.ascontiguousarray(np.rot90(voucher(1100, 480, 6, frame=True)))   # 480 high, 1100 wide, text sideways
    up = voucher(1200, 420, 7)
    p = page((side, 150, 200), (up, 1500, 150))
    assert_found(split_page(p), [[150, 200, 1250, 680], [1500, 150, 1920, 1350]])


@pytest.mark.xfail(strict=True, reason="known limit: a sideways voucher with no printed rule across its text columns "
                                       "is cut between the columns (seen on sideways telecom bills in the real pages)")
def test_rotated_voucher_without_rules_is_one_box():
    side = np.ascontiguousarray(np.rot90(voucher(1100, 480, 6)))
    assert_found(split_page(page((side, 150, 200))), [[150, 200, 1250, 680]])


def test_pen_note_on_the_page_is_not_a_voucher():
    p = page((voucher(1000, 900, 8), 200, 200))
    cv2.putText(p, "112/9-10", (1500, 1400), cv2.FONT_HERSHEY_SIMPLEX, 3, (30, 60, 220), 6)   # blue pen (RGB)
    assert_found(split_page(p), [[200, 200, 1100, 1200]])


def test_accepts_path_pil_and_grey_and_scales_with_resolution(tmp_path):
    p = page((voucher(1100, 1000, 9), 100, 100), (voucher(1100, 1000, 10), 1200, 100))
    truth = [[100, 100, 1100, 1200], [1200, 100, 2200, 1200]]
    f = tmp_path / "scan.png"
    ok, buf = cv2.imencode(".png", p[:, :, ::-1])
    buf.tofile(str(f))
    for src in (f, str(f), Image.fromarray(p), cv2.cvtColor(p, cv2.COLOR_RGB2GRAY)):
        assert_found(split_page(src), truth)
    half = cv2.resize(p, (PAGE_W // 2, PAGE_H // 2), interpolation=cv2.INTER_AREA)   # a 100 dpi scan
    assert_found(split_page(half), [[v // 2 for v in t] for t in truth])
