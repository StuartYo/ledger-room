# Strict reading: design, fixed before the real_tw2 run

Written 2026-11-07, before any reader has seen a real_tw2 photo. Do not tune it on real_tw2 results.

## Why

On the 41 real_tw photos (first run, 9B), Ledger Room let 5 photos with a wrong field through without a person;
after the blank-field fix, 3. A trained person keying by hand gets a field wrong about 0.1–0.3% of the time
(Garza et al., systematic review, single entry 0.29% per field), so roughly 1–2.4% of vouchers carry a wrong
field. Ledger Room's accepted vouchers must do at least as well, or the time it saves is spent finding its mistakes.

## The rule (one principle)

A value that goes into the filing file is accepted without a person only when two independent sources agree on it.
Sources: the OCR text on the page, a model's reading of the image, and a check digit (the UBN checksum). Anything
else goes to a person (READ_REVIEW).

| Field | Accepted when | Otherwise |
|---|---|---|
| Invoice number | OCR and a model read the same value (`ocr-confirmed`) | person |
| Date | OCR and a model read the same date (`ocr-confirmed`) | person |
| Seller / buyer UBN | OCR and a model agree, or a zoom vote of 2 or 3, or one OCR reading whose checksum is valid | person |
| Total | unchanged: two printed sources must agree (existing rule) | person |
| Voucher type | when a model alone says "plain receipt" and no invoice number was read, the type decides that no invoice number is needed; that decision rests on one source | person |

Default mode is unchanged. Strict mode is switched on by `LEDGER_STRICT_READ=1` or `pipeline.STRICT = True`.

## The second card (27B)

With strict mode, a voucher whose invoice number or date is not confirmed is also sent to the second reader (27B,
GPU.1), in addition to the existing reasons. The 27B reads the same OCR lines. When its reading of that field equals
the OCR value the first reading kept, the field counts as confirmed (`ocr-confirmed (second reader)`): OCR and two
models now agree. A 27B reading that differs changes nothing; the voucher goes to a person.

## How it is measured

- real_tw (41 photos): re-scored from the saved 9B reads of the 2026-11-06 official run. Labelled "after the fix,
  rule written after seeing this set's misses", never as a first run.
- real_tw2 (fresh photos, key frozen before any read): first official run, 9B alone, 9B strict, and 9B + 27B strict.
- Reported for each: field errors, photos with a wrong field that were not sent to a person, photos sent to a
  person, seconds per photo. The trade-off is stated plainly: strict mode sends more photos to a person.
