# Real photos: draft section (not in README yet)

Draft. Numbers marked TBD come from the first full run on the B70 host.

## Why

The synthetic sets show how Ledger Room behaves on layouts we generated. They cannot show how it reads paper that
someone else printed and photographed. This section adds real vouchers that anyone can download and check.

## Taiwanese vouchers (real_tw)

- 41 photos of Taiwanese e-invoice proofs (電子發票證明聯, 32 photos) and cash-register uniform invoices
  (收銀機統一發票, 9 photos), dated 2011 to 2025.
- Source: Wikimedia Commons. All 41 are CC BY-SA 3.0 or 4.0. Per-photo author, link and license are in
  `data/real_tw/ATTRIBUTION.md`. The copies in the repository are resized (long side at most 1600 px) and carry no
  EXIF data. They keep the license of their originals.
- Selection: a Commons search gave 649 candidate files. We drew 72 at random with a fixed seed. We dropped 31: files
  that are not a sales voucher, photos with several documents, and every photo that shows personal data (a person's
  name, including the owner's name in a seller stamp, a phone number, a card or member number, a vehicle plate).
  The reason for each dropped file is in the answer key.
- Answer key: `eval/expected_real_tw.json`. Fields: invoice number, date, seller UBN, total, and untaxed amount and
  tax when the voucher prints them. A field we could not read with confidence is marked `unreadable` and not scored.
- Frozen: `eval/frozen_real_tw.json` holds the SHA-256 of every photo, the key and `clients.json`. The key was frozen
  before any vision-language model read these photos. Model runs before the first full run were capped at 5 photos.

### How it is scored

`eval/real_photo_eval.py tw` compares three readers field by field:

- OCR only: OCR plus the same reconciliation code and rules, no model.
- Model only: the model's own transcription, with format clean-up only. Nothing is checked, so nothing goes to a
  person.
- Ledger Room: OCR, model and the code that cross-checks them, then the rules. A photo goes to a person when a hold or
  review rule fires on it.

For each reader it reports the field error rate, the photos with a wrong field that were not held, and the photos
sent to a person.

Results: TBD.

### Limits

- The labels were written by Claude, a different model from the readers under test, by looking at each photo. No
  person has checked them. A labelling error counts against every reader the same way.
- All 41 photos come from one Commons contributor. Most are flat scans or flat, well-lit photos on a plain
  background. They are easier than a phone photo taken at a counter.
- 41 photos is a small set. Only one voucher prints a separate untaxed amount and tax, so net and tax are barely
  tested. Triplicate invoices (三聯式) are missing: every one in the sample had a person's name in its stamp.
- Some Commons file names contain the invoice number. The readers never see the file name (photos are renamed).

### Found on the first real_tw run, fixed afterwards

The first full run showed photos where a required field came back blank and the photo was not sent to a person:

- TW013: invoice number blank (the key has LF16191539).
- TW014: invoice number and seller UBN blank.
- TW034: seller UBN blank.

The rules skipped a missing seller UBN without a word, and nothing checked a missing invoice number. A blank total
already went to a person; a blank invoice number or seller UBN did not.

What changed (`ledger_room/rules.py`, tests in `tests/test_missing_ids.py`): on a document that is supposed to print
both numbers, a blank invoice number or a blank seller UBN now raises READ_REVIEW ("Invoice number not found on the
page; fill it in by eye.", "Seller UBN not found on the page; fill it in by eye."). Those documents are uniform
invoices (e-invoice proof, triplicate, two-part register) and, for the seller UBN, a document of unknown type that
carries an invoice number. Plain receipts, payment slips and transaction details are not flagged: a taxi receipt
prints neither number, and slips are already held as NOT_A_VOUCHER. READ_REVIEW stays a routing note, outside the six
evaluated issue types.

The first-run numbers above remain the official ones. Any number computed after this change is labelled
after-the-fix. After-the-fix re-scores on cached reads (no model run):

- Synthetic main, heldout, heldout2 and the positive control (Qwen3.5-2B reads; OCR-only reads for main and
  heldout): no number moved (issue F1, documents with exactly the right flags, clean documents flagged, documents
  held).
- Hard set, OCR only: documents held 30 to 32. Both new holds are vouchers whose invoice number a person cannot read
  either (expect hold, not held: 3 to 1). Held although readable stays 18.
- real_tw, OCR only: photos sent to a person 38 to 39 (TW034).
- Not checked: whether the fix catches TW013 and TW014 in the model run. Their reads from that run are not on this
  machine. If the reader typed one of them as a plain receipt, this check does not fire on it.

## Foreign receipts (real_intl)

- CORD v2 (Indonesian receipts, CC BY 4.0): the validation and test splits, 200 photos, with the dataset's own total
  label. 193 of the 200 have one; the rest are skipped.
- Zenodo 13688441 (hand-held photos of restaurant receipts, CC BY 4.0): 193 photos with a transcription. The total
  is parsed from the line that starts with TOTAL. 127 receipts have exactly one such amount.
- The images stay outside the repository. Sources and credits: `data/real_intl/SOURCES.md`.

### What this tests

Only the total. Taiwanese rules (UBN checksum, invoice period, duplicates, Art. 19) do not apply to these receipts.
Ledger Room's own reader is not run on them: its prompt and labels are Taiwanese and its amount parser reads
"60.000" as 60. We test the idea behind its total check instead: accept a total only when two printed sources agree.
The script compares the OCR total row alone, the model's answer alone, and the two together, where a receipt goes to
a person when they disagree.

Results: TBD.

### Limits

- The Zenodo totals are parsed from a transcription, not labelled. Treat them as weaker evidence than CORD.
- The OCR + model check here is a short re-implementation in the evaluation script, not Ledger Room's code. It shows
  whether the idea carries over to foreign receipts. It does not measure Ledger Room itself on them.
