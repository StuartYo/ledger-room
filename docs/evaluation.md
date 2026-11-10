# Evaluation: method, laptop results and reproduction

## Model choice and Stage II targets

Why this path: it runs on the laptop's Intel UHD iGPU today through the same OpenVINO GPU plugin that drives Intel Arc,
and it follows the toolchain of ASUS's own Arc Pro B70 sample (OpenVINO + Qwen3.5 vision models). The measured
alternatives are in the results block below.

**Intel Arc Pro B70 path.** This paragraph is the Stage I reasoning, written on the laptop before any B70 run; what
the B70 then measured is under [b70_measurements.md](b70_measurements.md). What
the laptop showed (see [Results](#results)): the 2B reader is fast enough even on the laptop iGPU, and since code picks
the amounts it ends with every key field right on the synthetic held-out set. Real photos are harder: on the 10 real
receipts the 2B still sends 6 of 15 captures to a person, the 9B (`OpenVINO/Qwen3.5-9B-int4-ov`, same code, thinking
mode off) 4 of 15. On the laptop the 9B only ran on the CPU (compiling it for the iGPU ran out of memory, since the
iGPU shares system RAM), at more than a minute per document. That is why the B70 is in the plan: 32 GB of dedicated memory holds the 9B (5.9 GB) or a
27B INT4 reader (about 15 GB) next to the KV cache, so either reader fits with room to spare. The
code switches with `LEDGER_MODEL_DIR` and `LEDGER_DEVICE=GPU`.

Stage II targets on the B70, set now so they can be checked:

| Target | 9B INT4 reader | 27B INT4 reader |
|---|---|---|
| Key fields wrong, held-out | no worse than the laptop 9B plus 1 point | below the 9B |
| Ledger Room micro F1, held-out | at least 0.95 | at least 0.95 |
| Read time per document, whole pipeline (not yet verified) | pass 6 s, goal 3 s (600 / 1,200 per hour; goal: 12,000 documents in one night) | at most 12 s |
| Pure-model baseline with the same model | reported next to Ledger Room | reported next to Ledger Room |

Errata, not a change: the table above is frozen as written on 2026-10-14 and its thresholds stay as they are. "(not
yet verified)" in the read-time row is out of date; the result is below.

Result against those targets on the B70, held-out set, code 3524145 (`eval/results_b70.json` → `targets`):

| Target | 9B INT4 | 27B INT4 |
|---|---|---|
| Key fields wrong, held-out | 0/319: pass | 0/319: **missed**. "Below the 9B" cannot be met when the 9B has 0 wrong |
| Ledger Room micro F1, held-out | 1.00: pass | 1.00: pass |
| Read time per document, whole pipeline | 3.7 s: passes the 6 s line, **misses the 3 s goal** | 5.2 s: pass |
| Pure-model baseline with the same model | F1 0.20, reported | F1 0.47, reported |

## Offline check

**Offline.** The only step that needs the internet is the one-time model download. OCR, the model and the rules run
in-process, and the web app loads no external scripts, fonts or images.

An earlier version of this section said OpenVINO's telemetry was only used by the `ovc` model-conversion tool, which
Ledger Room never calls. That was wrong. `import openvino` imports `ovc` as well, and `ovc` sends one anonymous
"import" event to Google Analytics when the machine has no telemetry consent file. `eval/offline_check.py` caught it
on 2026-11-02 (a DNS lookup of www.google-analytics.com and one HTTPS connection; `eval/results_offline_before_fix.json`).
The event carries no document data. A spot check on 2026-10-14 had missed it because it sampled connections only
every 6 seconds.

The fix is in `ledger_room/__init__.py`: before any module imports OpenVINO, Ledger Room sets `CI=true` for its own
process. The telemetry package treats that as "declined" (`OptInChecker._run_in_ci` in
[openvinotoolkit/telemetry](https://github.com/openvinotoolkit/telemetry); `ovc` calls it with `disable_in_ci=True`).
OpenVINO's [documented switch](https://docs.openvino.ai/2026/about-openvino/additional-resources/telemetry.html),
`opt_in_out --opt_out`, writes a per-user setting instead; the host install scripts still run it, but Ledger Room
does not depend on it. `tests/test_telemetry_off.py` imports the OCR module with `CI` unset and checks that no
Python network call is made.

Check after the fix, 2026-11-02, laptop with Wi-Fi on and no consent file: all 47 held-out documents read live
(model load, OCR, model, rules, evidence crops, audit log, export). For 600.7 s, psutil sampled the process's
connections every second (589 samples) and a Python audit hook logged every socket connect and DNS lookup: 0
connections and 0 lookups to a non-local address (`eval/results_offline.json`). The audit hook only sees calls made
from Python and the sampler can miss a native connection shorter than a second, so the run with the network adapter
disabled is still on the Stage II list.

## Reproduce the evaluation

- **Full run (loads the model, roughly 50 minutes on the laptop).** `.venv\Scripts\python -m eval.run_eval`, then
  `.venv\Scripts\python -m eval.report`. This runs Ledger Room and the pure-model baseline on both sets, the oracle
  control and the positive control, and rewrites `eval/results.json`, `eval/RESULTS.md` and the results block below;
  `pytest` then confirms they agree.
- **Re-score only (seconds).** After a full run, `.venv\Scripts\python -m eval.run_eval --score-only` re-scores from
  the cached reads (`data/out/cache`) and pure-model answers (`data/out/baseline`). Delete those two folders to force
  fresh reads. Reads on the Intel GPU are not bit-identical between runs: in one repeat of the held-out set, two
  documents came back with different item lines or totals, while all 24 exceptions stayed the same.
- **Larger reader (held-out only, about 2 hours on the laptop CPU).**
  `.venv\Scripts\python -m eval.run_larger --model-dir E:\models\Qwen3.5-9B-int4-ov --device CPU` writes
  `eval/results_larger.json` (Ledger Room and the pure-model baseline with the 9B, reading accuracy, memory);
  `eval.measure_reader` records a reader's peak memory on a few documents; `run_eval --score-only` then merges both.
- **OCR only, no model (about a minute).** `.venv\Scripts\python -m eval.ocr_only` runs both synthetic sets with the
  same OCR, reconciliation code and rules but no vision-language model, and writes `eval/results_ocr_only.json`
  (merged by `run_eval`). `eval.real_check --ocr-only` does the same on private photos.
- **Real e-invoice check.** `.venv\Scripts\python -m eval.einvoice_check EXPORT.csv ... --out DIR` runs the import and
  the rules on MOF platform exports and writes aggregates only. Keep the exports and `DIR` outside the repository;
  `eval/results_einvoice_check.json` holds the aggregates of the one run reported here.
- **Period check (seconds, no model).** `.venv\Scripts\python -m eval.period_check --synthetic` runs the period
  check on the synthetic firm period in `data/period_synthetic/` (`synth/period_check.py`) and scores it against
  `eval/expected_period_check_synthetic.json`, which the generator wrote and which was committed before the checker
  existed; it writes `eval/results_period_check.json`. On real data: `python -m eval.period_check --ubn UBN --period
  2026-09/2026-10 --platform-root DIR [--filed-root DIR] [--paper result.json] --out FILE` writes counts only (no
  invoice numbers, UBNs or messages) and refuses an output path inside the repository.
- **Totals before and after (cached reads).** `.venv\Scripts\python -m eval.totals_eval --set main` (or `heldout`)
  compares the current amount code with the previous one, the model's own text and the pure model, and writes
  `eval/results_totals.json` (merged by `run_eval`). The held-out set 2 entry is marked frozen: the script refuses to
  run that set again.
- **Offline check (about 10 to 20 minutes).** `.venv\Scripts\python -m eval.offline_check` reads the held-out set live
  while it watches the process's network use, and writes `eval/results_offline.json`.
- The first blind held-out run is stored in `eval/results_blind_first_run.json`; `run_eval` merges it into
  `results.json` so the blind numbers are never overwritten by later runs.

## Synthetic data and evaluation

- `data/main`: 52 documents, 5 fictitious clients, layout family A. `data/heldout`: 47 documents, 5 other
  fictitious clients, layout family B (other fonts, column order, landscape receipts, rotation, uneven light).
  Both answer keys (`eval/expected_*.json`) were committed before any prompt or code tuning; tuning used `main` only.
- `data/heldout2`: 55 documents, 5 new fictitious clients, layout family C (cash-register e-invoice details,
  receipts, triplicate invoices with a separate total box; cash paid, change, card balances, deposits and order
  numbers printed near the total), new seed. Its key was committed (862a875) before the total-reading change and the
  set has been run once (see Results). The author designed it knowing what the change had to handle.
- Planted issues per set: bad UBN checksums, re-submitted vouchers across months, one voucher under two clients,
  VAT Act Art. 19 items, over-limit documents, wrong-period documents, plus clean documents. Some clean documents carry
  UBNs that pass the 2023 rule but fail the old one, to catch checkers that still use mod 10.
- **Planted rate.** 24 of the 52 main documents and 24 of the 47 held-out documents carry a planted issue, about
  half. Nobody knows that rate for real client folders; it is probably much lower, so precision and F1 on real data
  will differ from the numbers below.
- `eval/run_eval.py` compares Ledger Room with a **pure-model baseline**: the same model, same photo, same OCR lines,
  with the rules, the client's policy and the filing period in the prompt, plus a running list of the documents it
  already reviewed (built from its own answers). No code checks. `eval/run_larger.py` repeats both on the held-out
  set with a larger reader (Qwen3.5-9B), so the gap is not just a small-model effect.
- **Reading accuracy** (`eval/read_accuracy.py`): the fields Ledger Room ends up with, compared with the answer key,
  over eight key fields (type, invoice number, date, seller and buyer UBN, net, tax, total), plus whether every item
  line in the key was read.
- **Read time per document** means one thing everywhere (README, RESULTS.md, deck, video): OCR + model + zoomed
  re-reads for one document, averaged over a whole set. The 2B figures add the OCR and model time of the cached live
  reads (the held-out one was recorded for the video, with screen capture running on the same machine) to the time
  of any follow-up question and zoomed re-read the current code made on them.
- Controls: an **oracle** run (rules on the ground-truth fields, must be 100%) and a **positive control** (a clean
  batch, then the same batch with one planted change per issue type).
- Every number below is generated from `eval/results.json` by `python -m eval.report`; a test fails if they differ.

## Results

**Laptop numbers (Stage I).** Everything from here to the end of the runtime table was measured on the laptop
(Intel Core i7-13620H, Intel UHD iGPU, 9B on the CPU), not on the B70. B70 numbers are in
[b70_measurements.md](b70_measurements.md).

<!-- RESULTS:START (generated by eval/report.py from eval/results.json) -->
Synthetic data. Same local model (Qwen3.5-2B INT4, OpenVINO) on the same photo + OCR text in both columns; the pure model gets the rules in its prompt but no code checks.

| Set | Ledger Room micro F1 | Pure model micro F1 | Read time per document (laptop iGPU) |
|---|---|---|---|
| main (52 docs, layout A) | 1.00 (P 100%, R 100%) | 0.17 (P 10%, R 50%) | 16.9 s |
| heldout (47 docs, layout B) | 1.00 (P 100%, R 100%) | 0.15 (P 9%, R 42%) | 11.6 s |

**Held-out, first blind run** (code 5d02c9b, before any change made after seeing held-out errors): Ledger Room F1 0.96 (P 100%, R 92%), pure model F1 0.15. The table above uses later code with four fixes made after that run (listed in eval/RESULTS.md), so its held-out row is no longer blind.

**Amounts.** Code picks the total, net and tax from what is printed; the model's own reading is one vote (next paragraph). Held-out, fields wrong after reconciliation: net 0/32, tax 0/32, total 0/47. No document is left with a materially wrong or missing amount, so the amount rule (net + tax = total, tax = 5% of net, both read on a deductible voucher) has nothing to hold; documents held although their amounts were right: 0. Routing flags on held-out: AMOUNT_MISMATCH 0, BUYER_NOT_CLIENT 4, BUYER_PRESUMED 0, NOT_A_VOUCHER 0, SMALL_RECEIPT_CAP 0, READ_REVIEW 0.

**Totals picked by code** (eval/totals_eval.py). Code collects the printed evidence for the total (the 總計 / 合計 / 應付 row, the e-invoice random-code line, a net + 5% tax = total triple, the amount in Chinese numerals, the subtotal) and ignores numbers on cash, change, balance, card, deposit, points and reference lines. The model's own total, and its answer to a follow-up question asked only when the evidence is thin, count as one vote each, and only if that number is printed on the page. A total needs two agreeing sources and no tie; otherwise it is marked unconfirmed and the document goes to a person (READ_REVIEW). "Before" is the previous code on the same model replies.

Held-out set 2 (55 docs, layout family C: cash-register e-invoice details, receipts, separate total boxes; cash, change, card balances, deposits and order numbers printed next to the total). Its key was frozen before the change. Run record: first and only run of heldout2 (frozen at 862a875), code 59372db. It is not run again:

| | After | Before | Model's own text | Pure model |
|---|---|---|---|---|
| Totals right | 55/55 | 51/55 | 37/55 | 44/55 |
| Net right | 35/35 | 16/39 | 16/39 | n/a |
| Tax right | 35/35 | 23/39 | 23/39 | n/a |
| Cash, change, balance or deposit taken as the total | 0 | 2 | 1 | 2 |
| Amount wrong and not held | 0 | 2 | n/a | 11 (it holds nothing) |
| Documents held for a person | 0 | 22 | n/a | n/a |
| Over limit (right / false alarm / missed) | 5 / 1 / 0 | 5 / 1 / 0 | n/a | 2 / 17 / 3 |
| Six issue types, micro F1 | 0.98 | 0.94 | n/a | 0.16 |

Follow-up questions about the total: 3 of 55 documents. This set was drawn by the same author who wrote the fix, knowing what the fix had to handle, and its print is clean, so these numbers overstate what to expect on real photos.

Sets that are not blind for this change:

| Set | After: totals, net, tax right | Before | Model's own total | Pure model total | Held for a person after the change |
|---|---|---|---|---|---|
| main (used while writing the fix) | 52/52, 33/33, 33/33 | 52/52, 16/44, 22/36 | 46/52 | 43/52 | 0 (before 20) |
| heldout (used while writing the fix) | 47/47, 32/32, 32/32 | 41/47, 16/40, 24/36 | 36/47 | 46/47 | 0 (before 20) |
| real photos, Qwen3.5-2B-int4-ov (seen during development) | 9/9 | 5/9 | 4/9 | 8/9 | 6/15 (before 8/15) |
| real photos, Qwen3.5-9B-int4-ov (seen during development) | 9/9 | 7/9 | 6/9 | 9/9 | 4/15 (before 6/15) |

Real photos, totals wrong and not held, after the change (before): Qwen3.5-2B-int4-ov 0 (3); Qwen3.5-9B-int4-ov 0 (2).

**Real photos** (the owner's own receipts, two phone photos; answer key written by eye and frozen before any model ran; photos and key stay private, aggregates only):

| Reader | Receipts | Key fields right | Totals right (model alone) | Ready to deduct (expected 0) | Same receipt twice flagged (model alone) | Detail + payment slip flagged | Non-vouchers held | UBN false alarms (model alone) |
|---|---|---|---|---|---|---|---|---|
| Qwen3.5-2B-int4-ov | 10 (+5 repeats) | 41/49 | 9/9 (8/9) | 0/10 | 5/5 (0/5) | 1/1 | 3/3 | 0 (2) |
| Qwen3.5-9B-int4-ov | 10 (+5 repeats) | 42/49 | 9/9 (9/9) | 0/10 | 5/5 (3/5) | 1/1 | 3/3 | 0 (5) |
| ocr-only | 10 (+5 repeats) | 36/49 | 6/9 (n/a) | 0/10 | 5/5 (n/a) | 0/1 | 3/3 | 0 (n/a) |

Not blind for the slip columns: the first Qwen3.5-2B-int4-ov run (before the slip fix) caught the detail + payment slip pair 0/1, held 1/3 non-vouchers and raised 1 false UBN alarm (a payment terminal id read as a UBN). The fix (simplified-character keywords; slips paired with any document of the same date and amount; no UBN check on payment slips) left both synthetic sets unchanged. The totals column is not blind either: the code that picks totals was written after these photos had been read.

**Does a bigger model alone close the gap?** Held-out set (frozen key, no tuning), same laptop, Qwen3.5-9B-int4-ov on CPU:

| Reader | Ledger Room F1 | Pure model F1 | Reading error rate (key fields wrong) | Docs with all item lines right | Read time per document |
|---|---|---|---|---|---|
| Qwen3.5-2B-int4-ov (code c6c851c) | 1.00 | 0.15 | 0% (0/319) | 19/47 | 11.6 s |
| Qwen3.5-9B-int4-ov (code c445b35) | 1.00 | 0.18 | 6% (20/337) | 28/47 | 78.4 s |

The 9B row is from 2026-10-14 (code c445b35), before totals, net and tax were picked by code, and has not been rerun since; all 20 of its wrong fields are net, tax or total. The 2B row uses the current code, so the two reading error rates do not compare the readers fairly.

Key fields: type, invoice number, date, seller and buyer UBN, net, tax, total. Thinking mode off for both. The 9B ran on the laptop CPU because compiling it for the iGPU ran out of memory (the iGPU shares system RAM). On disk: 2B 1.99 GB, 9B 5.85 GB. Peak RAM in use (working set, eval/measure_reader.py): 2B on the iGPU 3.9 GB, 9B on the CPU 11.6 GB. 9B on the laptop CPU: about 46 documents per hour.

**What does the vision model add? OCR + the same code and rules, no model** (eval/ocr_only.py; code c6c851c):

| Set | Reader | Ledger Room F1 | Clean docs wrongly flagged | Key fields wrong | Docs held for a person | Read time per document |
|---|---|---|---|---|---|---|
| heldout | Qwen3.5-2B + OCR | 1.00 | 0/23 | 0% (0/319) | 0/47 | 11.6 s |
| heldout | OCR only | 1.00 | 0/23 | 10% (32/319) | 31/47 | 0.8 s |
| main | Qwen3.5-2B + OCR | 1.00 | 0/28 | 0% (0/345) | 0/52 | 16.9 s |
| main | OCR only | 1.00 | 0/28 | 1% (4/345) | 5/52 | 0.7 s |
| heldout | Qwen3.5-9B + OCR (code c445b35) | 1.00 | 0/23 | 6% (20/337) | 3/47 | 78.4 s |

Real photos (10 receipts, 5 repeat captures), key fields right / totals right / dates right / detail + payment slip paired / documents held for a person: Qwen3.5-2B-int4-ov 41/49 / 9/9 / 9/10 / 1/1 / 6/15; Qwen3.5-9B-int4-ov 42/49 / 9/9 / 9/10 / 1/1 / 4/15; ocr-only 36/49 / 6/9 / 7/10 / 0/1 / 15/15.

The six rules score the same with or without the vision model on both synthetic sets, because they run on OCR digits and code. The model matters for the amounts: its reading is the second vote that lets code confirm a total, so without it more key fields stay wrong and more documents go to a person (held-out: 10% (32/319) against 0% (0/319) wrong, 31 documents held against 0; main: 1% (4/345) against 0% (0/345), 5 held against 0). On the real photos the 2B reads dates 9/10 against 7/10 and totals 9/9 against 6/9, and pairs the meal detail with its payment slip (1/1 against 0/1; the pairing needs the date). The 9B row was measured with code c445b35, before totals were picked by code, so its reading error rate is not comparable with the rows above it. The OCR-only parser was written for this comparison against the synthetic label words and changed once after its first held-out run (folding simplified and traditional characters in the 銷售額 / 稅額 labels), so its held-out numbers are not blind.

**Real data, import path only (no OCR):** one person's own consumer e-invoices exported from the MOF platform (3 months; not a bookkeeping firm's client data; aggregates only). 30 invoices / 95 line items imported (7 with discount lines, 7 with zero-price lines). Seller UBNs passing the 2023 checksum: 23/23 (4 of them would fail an outdated mod-10 checker). Duplicates: 0. Art. 19 keyword hits: 2 invoices (alcohol); meal invoices: 12. Import plus rules: 0.085 ms per invoice.

Positive control: 7/7 planted changes detected, 0 false flags after, 0 flags before. Gate (stop if pure model >= 95% of Ledger Room): max ratio 0.18, pass. Details: [eval/RESULTS.md](../eval/RESULTS.md).

Runtime choice, measured on 2026-10-14 (Intel Core i7-13620H laptop, Intel UHD Graphics (iGPU), 32 GB RAM; same triplicate-invoice test image):

| Configuration | Time per document | Used |
|---|---|---|
| Qwen3.5-2B INT4, OpenVINO GenAI, CPU, full-size image (1144x844) | 43-48 s | no |
| Qwen3.5-2B INT4, OpenVINO GenAI, Intel iGPU, full-size image | 22-28 s | no |
| Qwen3.5-2B INT4, OpenVINO GenAI, Intel iGPU, image max side 896 px | 14-16 s | yes |
| Same + built-in MTP speculative decoding (2 draft tokens) | 14.5 then 38 s | no |
| OCR: PP-OCRv4 det+rec .onnx via ONNX Runtime 1.31, CPU | 17-28 s | no |
| OCR: Same .onnx files compiled by OpenVINO 2026.4, CPU | 0.3-1.1 s | yes |
<!-- RESULTS:END -->

**Period check, synthetic firm period** (`eval/results_period_check.json`, first run, code 2eef0cc; synthetic data, deterministic rules, no model; paper fields given as if read correctly). 4 clients, 140 e-invoices from platform downloads (2 allowances and 5 voided lines besides) and 44 paper vouchers. Planted errors caught: 18 of 18 (amount_diff 3/3, cross_client 3/3, fixed_asset 2/2, prior_filing_einvoice 3/3, prior_filing_paper 2/2, record_conflict 1/1, s19_einvoice 2/2, s19_to_confirm 1/1, void_paper 1/1). Vouchers held that should not have been: 0; planted holds missed: 0; rules raised where nothing was planted: 0. Ready to book: 167 (135 e-invoices, 32 paper; the key says 167), 13 held for a person, 6 photos booked from their platform line. The errors were planted by the same person who wrote the rules, so this shows the checks do what they say on the cases they were written for; it is not evidence about how often these errors occur in a real firm, and no real platform download has been checked yet.

## Test coverage notes

- Mostly synthetic photos. The real data so far: one person's own consumer e-invoices through the import path (no OCR)
  and 10 of his own receipts in two phone photos (laptop, results under [Results](#results)). Those 10 receipts are not
  a blind test: they were looked at while the slip checks and the total reading were written. On the B70: 41 + 17
  public Taiwanese receipt photos (real_tw, real_tw2) whose answer labels were written from the image only and frozen before runs, not checked by a person; 193
  CORD receipts scored on the total only; and the real company box, 336 vouchers of one small company, labelled by 4
  labelers from the images. No bookkeeping firm's client documents have been read, no licensed bookkeeper has checked any
  answer key, and no firm has piloted Ledger Room yet.
- Strict reading on real photos: 0 let through on real_tw is an after-the-fix number (the rule was written after
  seeing that set); real_tw2 is the only blind real-photo test of it, 17 photos. On the real company box strict
  reading still let 11 of 336 through before the utility-bill routing and 2 outside the bills after it (also after
  the fix).
- Totals. Code now picks every total right on the synthetic sets and on the 10 real receipts, but held-out set 2,
  the only blind check of that change, was designed by the author after the weakness was known and uses clean
  synthetic print, so it overstates real accuracy. On the laptop real photos the 2B reader still sends
  6 of 15 captures to a person, mostly when OCR finds the total in only one place.
- The expense category can come from the buyer's name. On held-out set 2 a receipt made out to a company named
  穗田便當有限公司 was taken as a meal and reported over the meal limit. It is not fixed in this version, because
  changing the rule after that run would leave held-out set 2 no longer blind.
- Utility bills. Of the 336 vouchers in the real company box ([data rule](real_data.md#real-data-one-companys-vouchers);
  counts in `eval/results_utility_detect.json`), 155 are utility bills. On OCR text alone
  the recogniser found all 155 and nothing else (155 found, 0 false, 0 missed). That is an after-the-fix number: the
  keywords were written after looking at that set. On the public sets (real_tw, real_tw2 and the four synthetic sets,
  259 documents, none of them a utility bill) it recognised none. The platform match is tested on synthetic records only.
- About half of the test documents carry a planted issue; real folders will have far fewer, so real precision will
  be lower.
- Only the 401 filing pair has passed the MOF checking program, and only on synthetic data; form 403 has not been
  checked. The voucher list has not been imported into any accounting software, and no bookkeeper has compared them with
  paper vouchers (see [stage2_log.md](stage2_log.md)). Form 404 is not supported. Deduction codes 2 and 4
  (fixed assets) are left to the bookkeeper. Until then the files are drafts for review.
- The vision model adds little to the decisions: with OCR and the same code and rules but no model, both synthetic
  sets score the same (see Results). Its measurable value so far is reading real photos (dates, pairing a detail with
  its payment slip) and, for the 9B, fewer misread fields.
- The planted issue types were designed by the author. The held-out sets change seed, clients and layouts, not the
  definitions of the issues.
- VAT Act Art. 19 categories are decided by an auditable keyword table first and by the model's suggested category only as a
  fallback (marked "medium" confidence). A real deployment needs the firm to review that table.
- Synthetic UBNs are random numbers that satisfy the checksum; the evaluation never loads the business register.
  The register lookup is tested on a small fixture only.
- Laptop timings use an Intel UHD iGPU. B70 timings come from runs of 47 to 53 documents and two 70-minute
  runs, not a night-long batch. The GPU driver error (`CL_OUT_OF_RESOURCES`) that once forced a reload every 5
  documents came from the UBN zoom strip; padded to 448 px, the 70-minute run had 0 restarts. The supervisor still
  starts a fresh reader if an error comes back. Not yet reported to OpenVINO.
- New layouts hurt reading: on blind3 (first official run) 40 of 326 key fields were wrong against 0 of 319 on the
  held-out layouts, and 2 documents were accepted with wrong UBNs. After the r9 changes blind3 gave
  18/326, but that run is no longer blind.
