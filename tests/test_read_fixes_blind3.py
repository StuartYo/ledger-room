"""Three reading fixes made after the first blind3 run (docs/blind3_draft.md). Every page here is a handful of
hand-made OCR lines (no model, no photo, no blind3 file): the situations are the ones real receipts produce.

1. An invoice number printed with a gap between its letters and digits ('AB　12345678') comes back from OCR as two
   lines; the 8 digits must stay the invoice number and never become a UBN. An OCR number in the buyer's place that
   the model disagrees with is not confirmed: it goes to the zoomed re-read and vote.
2. Amounts written one digit per box: OCR may keep one digit, or drop a 0. A labelled OCR number no longer overrides
   the model when the model's number is the one that makes net + tax = total (or is the full number OCR cut short).
3. A seller UBN equal to the client's own UBN goes to a person (seller and buyer were probably swapped).
"""
from datetime import date

from ledger_room import rules
from ledger_room.ocr import Line
from ledger_room.reader import (_truncated, amount_evidence, decide_net_tax, decide_total, ocr_tokens, postprocess,
                                split_invoice_numbers, ubn_roles)


def L(i, text, x0, y0, x1=None, h=30):
    return Line(f"L{i}", text, (x0, y0, x1 if x1 is not None else x0 + 18 * max(2, len(text)), y0 + h), 0.95)


# ---- 1. invoice number cut in two -----------------------------------------------------------------------------

def triplicate_header(letters="KT", digits="20481736", gap=30):
    """A triplicate invoice header: the number's letters and digits printed apart, the buyer block just below, the
    buyer UBN written in boxes OCR cannot read, the seller's stamp at the bottom."""
    return [
        L(1, "統一發票（三聯式）", 100, 70),
        L(2, letters, 850, 88, 892),
        L(3, digits, 892 + gap, 88, 892 + gap + 145),
        L(4, "買受人", 100, 150),
        L(5, "示範貿易有限公司", 210, 145),
        L(6, "統一編號", 670, 165),
        L(7, "統一發票專用章", 900, 800),
        L(8, "統一編號 85927754", 860, 760),
    ]


def test_split_invoice_number_is_the_invoice_not_the_buyer_ubn():
    lines = triplicate_header()
    assert [t for t, _, _ in split_invoice_numbers(lines)] == ["KT20481736"]
    inv, _ = ocr_tokens(lines)
    assert [t for t, _ in inv] == ["KT20481736"]
    roles = ubn_roles(lines)
    assert roles["buyer"] == [] and [t for t, _ in roles["seller"]] == ["85927754"]

    obj = {"type": "triplicate_invoice", "no": None, "seller_ubn": "85927754", "buyer_ubn": "00501503", "total": 0}
    fields, prov, boxes, extra = postprocess(obj, lines)
    assert fields["invoice_no"] == "KT20481736" and prov["invoice_no"] == "ocr"
    assert boxes["invoice_no"][0] == 850                  # the evidence box spans the letters and the digits
    assert prov["buyer_ubn"] == "needs-zoom"              # boxed digits: zoom in and vote, as before
    assert extra["buyer_ubn_model"] == "00501503"


def test_split_with_a_dash_and_a_model_that_copied_the_invoice_digits():
    lines = triplicate_header(letters="KT-")
    assert [t for t, _, _ in split_invoice_numbers(lines)] == ["KT20481736"]
    obj = {"type": "triplicate_invoice", "no": "KT20481736", "seller_ubn": "85927754", "buyer_ubn": "20481736"}
    fields, prov, _, extra = postprocess(obj, lines)
    assert fields["invoice_no"] == "KT20481736"
    assert extra["buyer_ubn_model"] is None               # the invoice number's digits are not a UBN
    assert prov["buyer_ubn"] == "needs-zoom"


def test_labels_and_far_or_other_row_letters_are_not_invoice_letters():
    tax_id = [L(1, "TAX ID", 100, 100, 200), L(2, "85927754", 215, 100, 360)]
    assert split_invoice_numbers(tax_id) == []
    other_row = [L(1, "KT", 100, 100, 140), L(2, "20481736", 150, 200, 295)]      # letters a row above
    assert split_invoice_numbers(other_row) == []
    far = [L(1, "KT", 100, 100, 140), L(2, "20481736", 600, 100, 745)]             # half a page apart
    assert split_invoice_numbers(far) == []
    word = [L(1, "PHONE", 100, 100, 190), L(2, "28825252", 200, 100, 345)]          # end of a longer word
    assert split_invoice_numbers(word) == []


def buyer_block(number="28825252"):
    """A lone 8-digit number printed next to the buyer block, with the buyer UBN itself in boxes."""
    return [
        L(1, "統一發票（三聯式）", 100, 70),
        L(2, "KT20481736", 850, 88),
        L(4, "買受人", 100, 150),
        L(5, "示範貿易有限公司", 210, 145),
        L(6, "統一編號", 670, 165),
        L(9, number, 400, 190, 545),
        L(7, "統一發票專用章", 900, 800),
        L(8, "統一編號 85927754", 860, 760),
    ]


def test_ocr_buyer_number_the_model_disagrees_with_goes_to_the_zoom_vote():
    lines = buyer_block()
    assert [t for t, _ in ubn_roles(lines)["buyer"]] == ["28825252"]
    obj = {"type": "triplicate_invoice", "no": "KT20481736", "seller_ubn": "85927754", "buyer_ubn": "00501503"}
    fields, prov, _, extra = postprocess(obj, lines)
    assert prov["buyer_ubn"] == "needs-zoom" and extra["buyer_ubn_ocr_role"] == "28825252"
    assert extra["buyer_ubn_model"] == "00501503"


def test_ocr_buyer_number_still_wins_when_model_agrees_or_copies_the_seller():
    lines = buyer_block()
    obj = {"type": "triplicate_invoice", "no": "KT20481736", "seller_ubn": "85927754", "buyer_ubn": "28825252"}
    fields, prov, _, _ = postprocess(obj, lines)
    assert (fields["buyer_ubn"], prov["buyer_ubn"]) == ("28825252", "ocr-confirmed")
    obj["buyer_ubn"] = "85927754"                         # the model copied the seller's number
    fields, prov, _, _ = postprocess(obj, lines)
    assert (fields["buyer_ubn"], prov["buyer_ubn"]) == ("28825252", "ocr-role")
    obj["buyer_ubn"] = None                               # nothing to disagree with
    fields, prov, _, _ = postprocess(obj, lines)
    assert (fields["buyer_ubn"], prov["buyer_ubn"]) == ("28825252", "ocr-role")


def test_disagreement_on_an_e_invoice_keeps_the_printed_number():
    """No boxes to zoom into on a printed e-invoice: OCR's digits stay (unchanged behaviour)."""
    lines = [L(1, "電子發票證明聯", 100, 60), L(2, "KT-20481736", 100, 100), L(3, "賣方 85927754", 100, 140),
             L(4, "買方 00501503", 400, 140)]
    obj = {"type": "e_invoice", "no": "KT20481736", "seller_ubn": "85927754", "buyer_ubn": "00501508"}
    fields, prov, _, _ = postprocess(obj, lines)
    assert (fields["buyer_ubn"], prov["buyer_ubn"]) == ("00501503", "ocr-role")


# ---- 2. amounts one digit per box -----------------------------------------------------------------------------

def boxed(label, digits, y, i0, x_label=100, x0=830, pitch=40):
    """A row of the amount table: the label at the left, then one OCR line per digit box (None = OCR missed it)."""
    out = [L(i0, label, x_label, y)]
    for k, ch in enumerate(digits):
        if ch is not None:
            out.append(L(i0 + 1 + k, ch, x0 + k * pitch, y, x0 + k * pitch + 18))
    return out


def test_one_digit_per_box_amounts_take_the_models_full_numbers():
    lines = boxed("銷售額合計", "6200", 500, 10) + boxed("營業稅", ["3", "1", "0"], 550, 20, x0=870) \
        + boxed("總計", ["6", "5", None, "0"], 600, 30)
    ev = amount_evidence(lines, "triplicate_invoice")
    assert ev["net_label"] == [6] and ev["tax_label"] == [3]          # OCR keeps only the first digit
    total, how, _, detail = decide_total(ev, 6510, None)
    assert (total, how) == (6510, "unconfirmed") and detail["truncated_ocr"] == 6   # shown, still checked by a person
    assert decide_net_tax(ev, total, "triplicate_invoice", 6200, 310) == (6200, 310, "model-arith", "model-arith")
    # no total to check against: the 5% relation alone picks the model's pair
    assert decide_net_tax(ev, None, "triplicate_invoice", 6200, 310)[:2] == (6200, 310)
    # a wrong total and nothing that fits: a one-digit OCR value that starts the model's number is not kept
    assert decide_net_tax(ev, 6, "triplicate_invoice", 6200, 310) == (6200, 310, "model-only", "model-only")


def test_ocr_dropping_a_zero_does_not_override_the_model():
    lines = [L(1, "銷售額", 100, 300), L(2, "96", 400, 300), L(3, "總計", 100, 380), L(4, "1,008", 400, 380)]
    ev = amount_evidence(lines, "e_invoice")
    assert ev["net_label"] == [96]
    assert decide_net_tax(ev, 1008, "e_invoice", 960, 48) == (960, 48, "model-arith", "model-arith")


def test_a_right_ocr_label_still_wins_over_a_wrong_model():
    lines = [L(1, "銷售額", 100, 300), L(2, "1,200", 400, 300), L(3, "營業稅", 100, 340), L(4, "60", 400, 340)]
    ev = amount_evidence(lines, "triplicate_invoice")
    assert decide_net_tax(ev, 1260, "triplicate_invoice", 12000, 60) == (1200, 60, "ocr-label", "ocr-label")
    assert decide_net_tax(ev, None, "triplicate_invoice", 1200, 60) == (1200, 60, "ocr-label", "ocr-label")
    # nothing fits, and OCR's value is not a cut-short version of the model's: OCR stays (unchanged behaviour)
    assert decide_net_tax(ev, 9999, "triplicate_invoice", 1300, 65)[:2] == (1200, 60)


def test_truncated_is_one_leading_digit_only():
    assert _truncated(6, 6200) and _truncated(2, 27773)
    assert not _truncated(5, 7)               # model number is one digit too
    assert not _truncated(5, 650)             # different first digit
    assert not _truncated(96, 960)            # two digits: may be a real amount, left to the arithmetic
    assert not _truncated(None, 960) and not _truncated(6, None)


# ---- 3. seller UBN = the client's own UBN -----------------------------------------------------------------------

CL = {"A": {"id": "A", "name": "甲公司", "ubn": "00501503", "period": ["2026-09-01", "2026-10-31"], "policy": {}}}


def folio(seller_ubn, buyer_ubn):
    return rules.Doc("f", "A", "2026-10", doc_type="e_invoice", invoice_no="KT20481736", date=date(2026, 10, 3),
                     seller_name="示範旅店", seller_ubn=seller_ubn, buyer_ubn=buyer_ubn, net=2286, tax=114, total=2400,
                     items=[rules.Item("住宿", 1, 2400)])


def test_seller_ubn_equal_to_client_ubn_goes_to_a_person():
    ex = rules.run_rules([folio("00501503", None)], CL)
    rr = [e for e in ex if e.rule_id == "READ_REVIEW"]
    assert len(rr) == 1 and rr[0].severity == "review" and "own UBN" in rr[0].message
    assert set(rr[0].evidence_fields) == {"seller_ubn", "buyer_ubn"}
    assert not any(e.rule_id == "UBN_CHECKSUM" for e in ex)


def test_normal_seller_and_client_as_buyer_gets_no_review():
    ex = rules.run_rules([folio("85927754", "00501503")], CL)
    assert not any(e.rule_id == "READ_REVIEW" for e in ex)
