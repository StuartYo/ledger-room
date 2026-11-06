# Draft: tiered reading on two cards

Status: draft for the README. Not measured on a B70 yet. The rule for "unsure" below was written down before any
tiered run and is fixed in code (`ledger_room/tiered.py`, rule version `t1`).

## What it does

The B70 host has two Arc Pro B70 cards. Until now Ledger Room used one of them. Tiered reading uses both.

1. The 9B reader runs on the first card (GPU.0) and reads every document in order.
2. When code is unsure about a document, it puts the document in a queue. The 27B reader runs on the second card
   (GPU.1) and reads the queued documents while the 9B goes on with the rest. The two cards work at the same time.
3. Code merges the two readings field by field. Then the same rules as before decide what is held.
4. A document goes to a person only when the two readings and OCR together still do not settle it.

The models only read. Code decides. The 27B reading is one more vote, not a final answer.

## When a document counts as unsure

A document goes to the 27B when the 9B reading has at least one of these:

- The model's reply is not readable JSON.
- The seller UBN, the buyer UBN or the total has provenance `model-only` or `unconfirmed`. For the total this means
  no two sources agree on it. These are the same fields and labels the rule engine already sends to a person
  (`READ_REVIEW`).
- Net plus tax does not equal the total, or the tax is not 5% of the net. This is the arithmetic check behind the
  existing `AMOUNT_MISMATCH` hold.

Nothing else sends a document on. A `model-only` date or invoice number does not, because the second reader sees the
same OCR lines and could not confirm those fields either.

## How the two readings are merged

Both readers get the same OCR lines. The 27B does not run OCR again, so both readings count the same printed sources.
Both go through the same reconciliation code.

- A field the 9B reading already confirmed on the page stays. The 27B cannot overwrite it.
- A field the 9B left weak (`model-only`, `unconfirmed` or `missing`) takes the 27B value only when the
  reconciliation code confirmed that value from the page, for example by an OCR match, a snap to printed digits, a
  zoom vote or two agreeing total sources.
- When the total comes from the 27B reading, net and tax come with it, since code derived them from that total.
- When net plus tax still does not match the total, the 27B net and tax are used only if the 27B read the same
  total and its net and tax pass the same arithmetic check.
- When the 9B reply did not parse, the 9B reading has no model vote. The 27B model values then fill the empty
  fields and keep their own provenance. A `model-only` UBN or total stays `model-only` and still goes to a person.

Each document keeps a `tier` record: whether it was sent on, why, which fields came from the 27B, and the 27B time.
The audit log records it with the read. If the 27B fails on a document, the 9B reading is kept and its unsure
fields go to a person as before.

## How it is measured

`python -m eval.bench_tiered --set heldout --set main --set private_real` on the B70 host writes
`eval/results_tiered.json`. New sets work the same way: `--set hard` reads `data/hard/` and scores against
`eval/expected_hard.json`.

For each set the script runs two modes with fresh reads (cache off):

- `first_only`: the 9B on GPU.0 alone.
- `tiered`: the 9B on GPU.0 and the 27B on GPU.1 together.

For each mode it records:

- documents a person must open (any hold or review exception);
- key fields wrong against the answer key (document type, invoice number, date, seller UBN, buyer UBN, net, tax,
  total);
- rule micro F1;
- wall time of the whole batch and documents per hour;
- busy seconds of each card's worker, and how much the two overlapped;
- how many documents went to the 27B, and for which reasons.

`private_real` has no answer key in the repository. For it the file holds counts and times only, with no document
ids, file names or values.

`measured_on_b70` is true for a set only when both cards are B70s, the run is not a dry run, every document of the
set was read, and no reader failed. A laptop dry run writes `eval/results_tiered_dryrun.json` with `dry_run: true`.
Its numbers check that the code runs. They are not results.

## Limits

- The rule for "unsure" was set from the code's own signals, not from data. It may send too few documents, or too
  many, on real photos. We will report what it does and not change it after seeing the numbers.
- On the synthetic held-out set the 9B alone already had no key field wrong on the B70 (2026-11-06). Tiered reading
  can only show a difference on harder sets or real photos.
- Both readers run in one process, in two threads. If the runtime holds Python's lock during a model call, the two
  cards would take turns. The results file reports the overlap so this can be checked.
