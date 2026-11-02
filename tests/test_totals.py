"""Total reading: code decides which printed number is the total; the model's numbers are votes.

All OCR lines here are hand-made (no model, no photo). They cover the cases seen on real receipts: cash paid and
change next to the total, a stored-value balance, a deposit, the e-invoice header slot, OCR dropping the 總 of 總計,
a tilted photo, and net + tax = total triples.
"""
import math

from ledger_room import rules
from ledger_room.ocr import Line
from ledger_room.reader import (amount_evidence, amount_tokens, cn_words_to_int, decide_net_tax, decide_total,
                                estimate_skew, postprocess)


def L(i, text, x0, y0, x1=None, h=26):
    return Line(f"L{i}", text, (x0, y0, x1 if x1 is not None else x0 + 18 * max(2, len(text)), y0 + h), 0.95)


def rows(*pairs, x_label=30, x_value=330, y0=100, dy=40):
    """Receipt rows: (label, value) printed left and right on the same row, as separate OCR lines."""
    out = []
    for k, (lab, val) in enumerate(pairs):
        y = y0 + k * dy
        out.append(L(2 * k, lab, x_label, y))
        out.append(L(2 * k + 1, val, x_value, y))
    return out


def total_of(lines, model_total=None, asked=None, dtype="receipt"):
    ev = amount_evidence(lines, dtype)
    return decide_total(ev, model_total, asked)


def test_cash_paid_is_never_the_total():
    lines = rows(("合計", "834"), ("現金", "1,000"), ("找零", "166"))
    v, how, needs_ask, d = total_of(lines, model_total=1000)
    assert d["rejected_payment_row"] == [1000]
    assert v == 834 and how == "unconfirmed" and needs_ask        # one source only: ask once
    v, how, needs_ask, _ = total_of(lines, model_total=1000, asked="834")
    assert (v, how, needs_ask) == (834, "ocr-asked+label", False)
    v, how, needs_ask, _ = total_of(lines, model_total=1000, asked="1000")   # both model reads took the cash
    assert (v, how, needs_ask) == (834, "unconfirmed", False)     # printed total kept, but a person checks it


def test_stored_value_balance_and_card_number_are_dropped():
    lines = rows(("應付金額", "520"), ("悠遊卡", "520"), ("卡號", "6403119827"), ("餘額", "386"))
    toks = amount_tokens(lines)
    assert {t["v"] for t in toks if t["label"] == "exclude"} == {520, 386}
    assert 6403119827 not in {t["v"] for t in toks}
    v, how, _, _ = total_of(lines, model_total=386, asked="520")
    assert (v, how) == (520, "ocr-asked+label")


def test_deposit_and_balance_due_are_not_the_total():
    lines = rows(("銷售額合計", "2,970"), ("營業稅額", "148"), ("總　計", "3,118")) + \
        [L(20, "已收訂金 1,000", 30, 300), L(21, "尾款 2,118", 30, 340), L(22, "總計新臺幣（大寫） 參仟壹佰壹拾捌元整", 30, 380)]
    ev = amount_evidence(lines, "triplicate_invoice")
    assert ev["arith"] == (2970, 148, 3118) and ev["words"] == 3118 and ev["label"] == 3118
    v, how, needs_ask, _ = decide_total(ev, 2118, None)
    assert v == 3118 and how == "ocr-arith+label+words" and not needs_ask
    assert decide_net_tax(ev, v, "triplicate_invoice", None, None)[:2] == (2970, 148)


def test_e_invoice_header_slot_with_dropped_label():
    # OCR dropped 總 from 總計 and the model returned 0; the 隨機碼 row still holds the total
    lines = [L(1, "電子發票證明聯", 60, 20), L(2, "2026-09-09 21:20:17", 20, 140), L(3, "隨機碼：5884", 20, 180),
             L(4, "計：334", 240, 182), L(5, "賣方：54373630", 20, 220), L(6, "餐0489台中復三14-600", 20, 400)]
    ev = amount_evidence(lines, "e_invoice")
    assert ev["slot"] == 334 and 600 not in ev["on_page"] and 5884 not in ev["on_page"]
    v, how, needs_ask, _ = decide_total(ev, None, None)
    assert v == 334 and needs_ask
    assert decide_total(ev, None, "334")[:2] == (334, "ocr-asked+slot")


def test_header_total_and_bottom_total_agree():
    lines = [L(1, "隨機碼:5425", 24, 200), L(2, "總計:6,640", 260, 200)] + \
        rows(("銷售額", "6,324"), ("稅額", "316"), ("應付金額", "6,640"), ("現金", "7,000"), ("找零", "360"), y0=600)
    ev = amount_evidence(lines, "e_invoice")
    v, how, needs_ask, _ = decide_total(ev, 7000, None)
    assert v == 6640 and not needs_ask and how.startswith("ocr-arith")
    assert decide_net_tax(ev, v, "e_invoice", None, None)[:2] == (6324, 316)


def test_tilted_photo_keeps_labels_on_their_rows():
    # labels at x=40, values at x=850, rows 44 px apart, photo tilted 4 degrees: the value of row k sits at the
    # height of the label of row k+1 unless the tilt is taken into account
    t = math.tan(math.radians(4))
    pairs = [("銷售額合計", "4,430"), ("營業稅 應稅 零稅率 免稅", "222"), ("總　計", "4,652")]
    lines = []
    for k, (lab, val) in enumerate(pairs):
        y = 300 + 44 * k
        lines.append(L(2 * k, lab, 40, int(y + 40 * t)))
        lines.append(L(2 * k + 1, val, 850, int(y + 900 * t), 930))
    lines += [L(10, "品名", 40, 100), L(11, "金額", 760, int(100 + 760 * t), 820)]
    assert abs(estimate_skew(lines) - t) < 0.01
    ev = amount_evidence(lines, "triplicate_invoice")
    assert ev["label"] == 4652 and ev["net_label"] == [4430] and ev["tax_label"] == [222]


def test_tokenizer_drops_dates_times_ids_and_quantities():
    lines = [L(1, "2026-09-22 13:26:22", 20, 20), L(2, "電話：06-8376-7600", 20, 60), L(3, "交易序號 723278786443", 20, 100),
             L(4, "影印紙 A4 一箱 2 x 120", 20, 140), L(5, "中華民國115年09月14日", 20, 180), L(6, "3,740TX", 300, 220),
             L(7, "總言17.300", 20, 260)]
    vals = {t["v"] for t in amount_tokens(lines)}
    assert vals == {120, 3740, 17300}


def test_amount_in_words():
    assert cn_words_to_int("參仟壹佰壹拾捌元整") == 3118
    assert cn_words_to_int("合計新臺幣 壹萬零伍佰元整") == 10500
    assert cn_words_to_int("伍萬壹仟参佰柒拾陸元整") == 51376      # simplified 参 folded
    assert cn_words_to_int("肆仟佰伍拾贰元整") is None             # OCR dropped a digit: not a clean amount


def test_plain_receipt_has_no_net_or_tax():
    lines = rows(("海鮮合菜6人份", "2,480"), ("炒青菜", "220"), ("合計", "2,700"))
    fields, prov, _, extra = postprocess({"total": 2700, "net": 2700, "tax": 0}, lines)
    assert (fields["net"], fields["tax"], fields["total"]) == (None, None, 2700)
    assert prov["total"] == "ocr-label+model" and not extra["total_needs_ask"]


def test_unconfirmed_total_is_held_for_a_person():
    d = rules.Doc(doc_id="X/2026-09/a.jpg", client_id="X", month="2026-09", doc_type="receipt", total=834,
                  uncertain={"total": "unconfirmed"})
    ex = rules.run_rules([d], {"X": {"id": "X", "name": "x", "ubn": "12345675", "policy": {},
                                             "period": ["2026-09-01", "2026-10-31"]}})
    rr = [e for e in ex if e.rule_id == "READ_REVIEW"]
    assert len(rr) == 1 and rr[0].evidence_fields == ["total"] and rr[0].severity == "review"
