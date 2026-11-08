"""Field boxes around the value only (ledger_room.tight_boxes): span finding, turned rectangles, one real crop."""
import math
from pathlib import Path

import pytest

from ledger_room import tight_boxes as T

ROOT = Path(__file__).resolve().parents[1]


def test_find_span_numbers_ids_dates():
    s = "總計 NT$4,652 元"
    a, b = T.find_span("total", 4652, s)
    assert s[a:b] == "4,652"
    assert T.find_span("total", 4650, s) is None
    s = "字軌號碼 GQ-07058325"
    a, b = T.find_span("invoice_no", "GQ07058325", s)
    assert s[a:b] == "GQ-07058325"
    s = "統一編號：12345675"
    a, b = T.find_span("seller_ubn", "12345675", s)
    assert s[a:b] == "12345675"
    s = "開立日期 115 年 09 月 07 日"
    a, b = T.find_span("date", "2026-09-07", s)
    assert s[a:b].startswith("115") and s[a:b].endswith("日")
    a, b = T.find_span("date", "2026-09-14", "日期1150914")
    assert (a, b) == (2, 9)
    assert T.find_span("date", "2026-09-15", "日期1150914") is None


def test_rect_follows_the_text_turn():
    # four characters on a line turned by 5 degrees
    ang = math.radians(5)
    ux, uy, vx, vy = math.cos(ang), math.sin(ang), -math.sin(ang), math.cos(ang)
    quads = []
    for i in range(4):
        x0, x1 = 20 * i, 20 * i + 16
        quads.append([[x * ux + y * vx, x * uy + y * vy] for x, y in ((x0, 0), (x1, 0), (x1, 24), (x0, 24))])
    a = T._angle(quads)
    assert abs(a - 5) < 0.01
    q = T._rect([p for c in quads for p in c], pad=0, angle=a)
    assert abs(math.dist(q[0], q[1]) - 76) < 0.1 and abs(math.dist(q[0], q[3]) - 24) < 0.1
    # TL is the upper left corner on the paper, TR is to its right
    assert q[1][0] > q[0][0] and q[3][1] > q[0][1]


def test_items_keep_the_line_box():
    doc = {"doc_id": "x", "fields": {"items": [{"desc": "名片"}]}, "boxes": {"items": [10, 20, 200, 44]}}
    r = T.for_doc(Path("missing.jpg"), doc, {})
    assert r["items"]["how"] == "box" and r["items"]["box"] == [10, 20, 200, 44]


def test_real_crop_frames_the_number_not_the_label():
    img_path = ROOT / "data" / "heldout" / "clients" / "H01" / "2026-10" / "IMG_1765.jpg"
    if not img_path.exists():
        pytest.skip("demo image not present")
    pytest.importorskip("rapidocr_onnxruntime")
    from PIL import Image
    import json
    from ledger_room import pipeline as P
    res_p = P.OUT / "runs" / "heldout-20261014-r3-replay" / "result.json"
    if not res_p.exists():
        pytest.skip("demo run not present")
    img = Image.open(img_path).convert("RGB")
    d = next(x for x in json.loads(res_p.read_text(encoding="utf-8"))["docs"] if x["doc_id"].endswith("IMG_1765.jpg"))
    r = T.tighten(img, d["boxes"]["invoice_no"], "invoice_no", d["fields"]["invoice_no"])
    b = d["boxes"]["invoice_no"]                      # the reader's line box: label + invoice number
    assert r["how"] == "chars"
    assert r["box"][0] > b[0] + 40                    # the label "字軌號碼" is left out
    assert T._area(r["quad"]) < (b[2] - b[0]) * (b[3] - b[1])
