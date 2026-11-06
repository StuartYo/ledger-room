# Blind set 3: first run (draft)

The set itself is described in `docs/new_sets_draft.md`. The first and only official run is in
`eval/results_tiered.json` under `sets/blind3`: Qwen3.5-9B alone on the Intel Arc Pro B70, code `77fa568`,
2026-11-06.

## Found on the first blind3 run, fixed afterwards

**The first run's numbers are the official blind3 numbers and stay so.** Rule F1 0.836 (23 found, 5 false alarms,
4 missed), 40 of 326 key fields wrong, 11 of 53 documents sent to a person, 1 of 26 clean documents flagged.
Everything below was changed after seeing that run, so a later blind3 score would not be blind.

What the run showed (all false alarms and all misses were on one layout, the handwritten triplicate invoice):

1. **Invoice number cut in two.** The number was printed as two letters, a wide gap, then 8 digits
   (`RC　50461672`). OCR returned two lines, the invoice number was not recognised, and the 8 digits were taken as
   the buyer UBN because they sat near 買受人. This caused all 5 false UBN_CHECKSUM alarms and 3 of the 4 misses
   (a wrong buyer UBN makes the input tax look non-deductible, so the Art. 19 check is skipped). The zoomed re-read
   of the boxed buyer UBN, which works on this layout, never started: one OCR number in the buyer's place was taken
   as settled even when the model read a different number.
2. **Amounts written one digit per box.** OCR kept only the first digit (6 for 6,200), and a labelled OCR number
   always replaced the model's number. Net, tax and total came out wrong on all 9 handwritten invoices. All 9 went to
   a person anyway (total not confirmed, or net + tax not equal to total), but one OVER_LIMIT was missed and 3 clean
   documents were sent to a person only because of this.
3. **Hotel folio: seller and buyer swapped.** The client's own UBN, printed next to the company letterhead, was read
   as the seller UBN and the real buyer UBN was dropped. Only a warning fired, so these 2 documents did not go to a
   person. They were the only wrong values that no person would have seen.

What changed (reader version r9):

1. An 8-digit number alone on its OCR line, with two capital letters ending the line just to its left on the same
   row, is read as one invoice number, never as a UBN. On a triplicate invoice, when OCR finds one number in the
   buyer's place and the model reads a different one (not the seller's), neither is accepted: the boxed buyer UBN
   goes to the zoomed re-read, where two of three independent reads must agree. A model buyer UBN equal to the
   invoice number's digits is ignored.
2. When a labelled OCR amount and the model disagree, code keeps the pair that makes net + tax = total and
   tax = 5% of net, OCR first. If nothing fits, a one-digit OCR value that is the first digit of the model's number
   is replaced by the model's number. The same applies to the value shown for an unconfirmed total; it is still sent
   to a person.
3. New check: a seller UBN equal to the client's own UBN goes to a person (READ_REVIEW: seller and buyer were
   probably swapped).

Tests: `tests/test_read_fixes_blind3.py`, small hand-made OCR pages, no blind3 file.

**Checks after the change.** The cached laptop reads (OCR lines + model replies) were run again through the new
code: main and held-out (2B), held-out (9B), held-out 2, both control sets, and the OCR-only reader on the hard and
real Taiwan sets. No rule or reading number got worse. Two real Taiwan photos improved: one invoice number OCR had
split is now found (OCR-only), and one total read as 1 now shows 1,030 (still sent to a person).

**Blind3 after the fix (re-scored from the first run's reads).** Only change 3 can be re-scored without the model,
by running the rules again on the fields the first run read: rule F1 stays 0.836, 40/326 fields stay wrong,
documents sent to a person go from 11 to 13, and no document with a wrong value is left unseen (was 2).
Changes 1 and 2 alter the reading itself; scoring them needs the model's replies, which the first run did not keep,
so they need another run on the B70. That run would not be blind.
