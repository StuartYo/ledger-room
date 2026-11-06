"""real_tw is frozen, licensed and free of model output; the scoring and amount parsing in eval/real_photo_eval.py
behave as documented. No network, no F: drive, no model."""
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

from eval.real_photo_eval import LABEL_NOTE, TW_FIELDS, ocr_total, score_tw, to_amount
from ledger_room import ubn
from ledger_room.ocr import Line

ROOT = Path(__file__).resolve().parents[1]
EXP = json.loads((ROOT / "eval" / "expected_real_tw.json").read_text(encoding="utf-8"))
FROZEN = json.loads((ROOT / "eval" / "frozen_real_tw.json").read_text(encoding="utf-8"))
h = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()   # noqa: E731


def test_real_tw_matches_freeze():
    for k in ("expected", "clients_json", "attribution"):
        assert h(ROOT / FROZEN[k]["path"]) == FROZEN[k]["sha256"], k
    assert set(FROZEN["images"]) == {d["doc_id"] for d in EXP["docs"]}
    for d in EXP["docs"]:
        p = ROOT / "data" / "real_tw" / "clients" / d["doc_id"]
        assert h(p) == d["sha256"] == FROZEN["images"][d["doc_id"]], d["doc_id"]


def test_answer_key_shape():
    assert EXP["note"] == LABEL_NOTE and EXP["synthetic"] is False
    assert EXP["n_docs"] == len(EXP["docs"]) >= 30
    for d in EXP["docs"]:
        for f in TW_FIELDS:
            v = d[f]
            assert v is None or v == "unreadable" or (isinstance(v, int) if f in ("total", "net", "tax") else isinstance(v, str)), (d["file"], f)
        if isinstance(d["seller_ubn"], str) and d["seller_ubn"] != "unreadable":
            assert ubn.is_valid_new(d["seller_ubn"]) or ubn.is_valid_old(d["seller_ubn"]), d["file"]
        if isinstance(d["invoice_no"], str) and d["invoice_no"] != "unreadable":
            assert re.fullmatch(r"[A-Z]{2}\d{8}", d["invoice_no"]), d["file"]


def test_every_photo_is_attributed_with_an_allowed_license():
    att = (ROOT / "data" / "real_tw" / "ATTRIBUTION.md").read_text(encoding="utf-8")
    rows = [l for l in att.splitlines() if l.startswith("| TW")]
    assert len(rows) == len(EXP["docs"])
    for d in EXP["docs"]:
        row = next(l for l in rows if l.startswith(f"| {d['file']} "))
        assert "commons.wikimedia.org/wiki/File:" in row
        assert re.search(r"\[(CC0|CC BY(-SA)? \d\.\d|Public domain)\]", row), row


def test_photos_are_small_and_carry_no_exif():
    from PIL import Image
    for d in EXP["docs"]:
        im = Image.open(ROOT / "data" / "real_tw" / "clients" / d["doc_id"])
        assert max(im.size) <= 1600 and not im.getexif(), d["file"]


def test_score_tw_counts_misses_holds_and_unprinted_fields():
    truth = [{"file": "a.jpg", "invoice_no": "AB12345678", "date": "2020-01-02", "seller_ubn": None, "total": 100,
              "net": None, "tax": None},
             {"file": "b.jpg", "invoice_no": "CD12345678", "date": "unreadable", "seller_ubn": "04595257", "total": 50,
              "net": 48, "tax": 2}]
    got = {"a.jpg": {"invoice_no": "AB12345678", "date": "2020-01-02", "seller_ubn": "12345678", "total": 100, "net": 95},
           "b.jpg": {"invoice_no": "CD12345678", "date": None, "seller_ubn": "04595257", "total": 52, "net": 48, "tax": 2}}
    s = score_tw(truth, got, held={"a.jpg"})
    assert s["fields_scored"] == 8 and s["fields_wrong"] == 1
    assert s["values_not_printed"] == {"seller_ubn": 1}          # net on a.jpg is not counted: net/tax only when printed
    assert s["fields_unreadable_not_scored"] == 1
    assert s["photos_sent_to_a_person"] == 1 and s["wrong_and_not_held"] == 1 and s["held_with_all_fields_right"] == 1
    m = score_tw(truth, got, held=None)
    assert m["photos_sent_to_a_person"] == 0 and m["wrong_and_not_held"] == 1


def test_amounts_in_any_format():
    assert to_amount("60.000") == Decimal(60000)
    assert to_amount("45,500") == Decimal(45500)
    assert to_amount("Rp. 35.000") == Decimal(35000)
    assert to_amount("1,234.50") == Decimal("1234.5")
    assert to_amount("$56.58") == Decimal("56.58")
    assert to_amount("236.00") == Decimal(236)
    assert to_amount("abc") is None and to_amount(None) is None


def test_ocr_total_takes_the_total_row_not_subtotal_cash_or_change():
    L = lambda i, t, y, x=10: Line(str(i), t, (x, y, x + 200, y + 20), 0.9)   # noqa: E731
    lines = [L(1, "SUBTOTAL", 100), L(2, "50.000", 100, 300), L(3, "TOTAL", 130), L(4, "55.000", 130, 300),
             L(5, "CASH", 160), L(6, "100.000", 160, 300), L(7, "CHANGE", 190), L(8, "45.000", 190, 300)]
    assert ocr_total(lines) == Decimal(55000)
    assert ocr_total([L(1, "Grand Total Rp 12.500", 100), L(2, "Total Item 3", 140)]) == Decimal(12500)
