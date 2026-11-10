# Two new synthetic test sets

Both sets are synthetic. The author wrote the programs that draw them, and the author knows the six rules. No real
company, person, receipt or photo is in either set. UBNs are random numbers that pass (or, where planted, fail) the
checksum.

## Hard set: `data/hard`, `eval/expected_hard.json`

**Why.** On the clean synthetic sets, OCR alone with the same code scores the same as OCR plus the vision model. So
those sets cannot show what the model adds. Real phone photos of paper are harder than clean prints.

**How it is made** (`synth/hard.py`). 49 clean vouchers come from the existing planner and the three existing layout
families (A, B, C) with a new seed and new clients. None carries a planted issue. Each one then gets one kind of
damage, drawn by code:

| Damage | Documents | What it looks like |
|---|---|---|
| handwritten | 7 | white-out over the total, rewritten by hand; or a digit written over another |
| crease_shadow | 7 | crumpled paper with fold lines, plus a cast shadow |
| perspective | 7 | a steep angle; two are also taken from far away |
| stamp_pen | 7 | a red PAID stamp, a pen loop or tick over the total, a marker scribble, an over-inked stamp |
| thermal_fade | 7 | faded thermal paper (e-invoices only) |
| low_light_blur | 7 | dim light, noise, motion blur |
| occlusion | 7 | a finger or another receipt over part of the page |

**What counts as right.** The key marks each key field as readable by a person or not. For local damage this is
computed: the layouts are rendered with their text calls recorded, so the code knows every place a value is printed
(the total also in Chinese numerals, the seller UBN also on the seal). A field stays readable if one printed copy is
at least 70% uncovered and not written over. For whole-page damage the author looked at every severe page and wrote
down which fields he could not read. 15 of 49 documents have a field nobody can read. For those the right outcome is
a hold for a person, not a lucky guess.

**What `eval/hard_eval.py` reports**, per damage type: key fields read wrong (only over readable fields), documents
held, documents that should have been held and were not, false alarms among the six issue types, and documents with
a field read as a wrong value that nothing held. The last number matters most.

**First numbers (laptop, OCR only, not the B70).** Fields wrong 9.9% of readable fields. 30 of 49 held. A wrong value
that was not held: 0. Should have been held but was not: 3. False alarms: 0. The model half has only run as a
four-document dry run with the 2B reader, to check the script. The model numbers will come from the B70.

**Known biases.**
- The layouts are the ones the code was developed on. Only the damage is new.
- The damage is simulated. The handwriting is a jittered Kai font, the finger is a drawn shape, and the creases are
  a warp of a flat image. Real paper and real cameras fail in other ways too.
- The readability calls are one person's, on one screen. The 70% threshold is a choice, not a standard.
- Every voucher is clean, so the set does not test whether damage hides a planted issue.

## Blind set 3: `data/blind3`, `eval/expected_blind3.json`

**Why.** The earlier held-out sets change seed, clients and layouts but come from the same generator, written next
to the code. A 24 of 24 score there can fairly be called "the author grading his own exam".

**How it is made** (`synth/blind3.py`). 53 documents, 5 new clients, 26 clean. Six new templates: an A4 e-invoice
printout, a 58 mm thermal e-invoice with service charge and discount lines, a handwritten triplicate invoice with one
box per digit, a taxi ride slip, a handwritten receipt pad and a hotel folio. New fonts, a new filing period
(115年07-08月 instead of 09-10), new expense limits, new date and amount spellings (115.07.14, NT$ 2,400.00, 3,600-).
The issues are the same six types, shown in new ways:

- UBN checksum: a wrong buyer UBN on an e-invoice, two swapped digits written by hand, a wrong UBN that appears only
  on a red seal or on a taxi slip.
- Same client twice: a reprint (補印) of an e-invoice, a photocopy of a triplicate, a taxi slip with no invoice number.
- Two clients: a thermal e-invoice and a taxi slip each filed by two clients.
- VAT Act Art. 19: gift boxes with the purpose in the remarks column, a flower basket for a client's opening, prizes
  for a staff party, tyres for the company's own passenger car, a business lunch marked 宴請客戶.
- Over limit: a total pushed over the meal limit only by a 10% service charge, one hotel night, a handwritten
  triplicate above the approval threshold, a handwritten meal receipt.
- Period: a date one day after the period, an e-invoice whose period header does not contain its date (both
  directions), a date before the period.

**How it was kept honest.**
- The answers were written by hand from the README rule table and the cited articles. The author did not read
  `ledger_room/rules.py`, `reader.py` or `pipeline.py` while writing them.
- The generator imports nothing from `ledger_room` or from `synth/generate.py`. Its UBN checksum was written again
  from the FIA notice. Before freezing it was compared with the project's checksum on 200,000 random numbers, not on
  set data: 0 differences.
- The set was frozen before anything read it. `eval/frozen_blind3.json` holds the SHA-256 of every image, the answer
  key, `clients.json` and the generator. A test fails if any of them changes. `python -m synth.blind3 --verify`
  re-renders the images and compares them.
- No model, no OCR and no Ledger Room code has been run on it. The only check before freezing opened each image and
  checked its size. The first and only scored run is planned on the B70. (Run on 2026-11-06; results and the fixes
  made after it: `docs/blind3.md`.)

**Known biases.**
- The author still knows the rules and wrote the code earlier, so he knows what kind of mistake it makes. Writing
  from the README instead of the code lowers this bias. It does not remove it.
- It is still clean synthetic print with mild photo effects. It tests the rules on new layouts, not reading of real
  paper.
- About half the documents carry an issue. Real folders carry far fewer.
- A few answers follow a convention from the README that a firm could set differently. The limit is per document.
  A total equal to the limit is not over it (one document tests exactly that). A date before the period, a meal
  under Art. 19 and a slip matched on seller, date and amount are warnings in the README; the key still lists them
  under their issue type, as the older keys do. A bakery's gift boxes are a gift, not a meal.
- File names carry the day the photo was taken, which can be later than the voucher date.

**Why blind3 is more believable than held-out 2, and why it is still not real data.** It was frozen with hashes
before any run, its generator does not share code with the system under test, and its questions came from the rule
text. That makes "tuned to the test" much harder to argue. It is still made by the same person, in the same six
categories, on clean synthetic print. Real client folders, read once under a frozen key, are the test that would
settle it.
