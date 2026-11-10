# Measurements on the Intel Arc Pro B70

All measurements in this file were taken on the organizers' host: an Intel Xeon 6787P with two Intel Arc Pro B70
cards, the same GPU as the ASUS UGen B70. Each number is copied from the results file named next to it; a test
(`tests/test_readme_b70.py`) fails if they differ. Summary tables: [README](../README.md#measured-on-the-organizers-intel-arc-pro-b70).

## Headline results in detail

**113 real invoices: the old ledger let 15 wrong rows through, Ledger Room let 0.**

Three headline facts, all on real documents (definitions: *let through* = a voucher with a wrong field accepted
without a person; *sent to a person* = held for a bookkeeper to check):

1. **The same 113 real invoices of one small company** ([data rule](real_data.md#real-data-one-companys-vouchers)).
   The company's old ledger (rows typed by a general chat AI, checked once by the owner): **15 wrong rows** (invoice
   number 7, date 6, total 3). Ledger Room, 9B on the B70, strict reading: **10 misread, all 10 held, 0 let through**;
   53 of 113 sent to a person. Compared on the three fields the old ledger keeps; on all six scored fields Ledger Room
   let 1 through, a seller tax ID (UBN), which the old ledger does not record.
   `eval/results_real_company.json` → `old_ledger_vs_ledger_room`
2. **Public real Taiwan photos, strict reading: 0 let through** on 41 (real_tw, re-scored after the fix: the strict
   rule was written after seeing this set) and 0 on 17 (real_tw2, first run, key frozen before any read).
   `eval/results_real_tw_strict.json`, `eval/results_real_tw2_strict.json` → `conditions.strict`
3. **On one Arc Pro B70: one card reads about 1,000 vouchers an hour and the 9B uses 8 GB of its 32 GB.** Same host
   and code, live rerun: the 9B takes 3.6 s a document on one B70 (GPU.1) and 9.8 s on the host's two 86-core Xeon
   CPUs (`eval/results_same_host_cpu_vs_b70_live.json`; an earlier run on GPU.0 gave 3.8 s,
   `eval/results_same_host_cpu_vs_b70.json`). The spare memory leaves room: an optional 27B second reader, which
   re-reads the ones the 9B is unsure about (on the real box it cut wrong fields from 21.5% to 18.2%), fits on the same
   card beside the 9B, peak 21.8 GB, 20 documents read by each, none failed (`eval/results_one_card.json`). In the web app the 9B is the only reader, and the 27B runs beside it
   on the same card as the review agent (see [Agent: a second look at held documents](how_it_works.md#agent-a-second-look-at-held-documents)). The bigger reader is also the
   accurate one. Same real documents, strict reading, Qwen3.5-2B against Qwen3.5-9B:

   | Same real documents, strict reading | 2B | 9B |
   |---|---|---|
   | real_tw, 41 photos: fields wrong / sent to a person / let through | 18 of 164 / 21 / 1 | 11 of 164 / 14 / 0 |
   | real_tw2, 17 photos (first run): fields wrong / sent to a person / let through | 19 of 66 / 14 / 0 | 15 of 66 / 9 / 0 |
   | 336 real vouchers: share of fields wrong | 36.1% | 21.5% |
   | 113 real invoices: sent to a person / let through | 91 / 1 | 53 / 0 |

   The 9B takes 78.4 s a document on the laptop CPU and 3.7 s on one B70 (held-out synthetic set; laptop code
   c445b35, B70 code 3524145). `eval/results_real_tw_strict_2b.json`, `eval/results_real_tw2_strict_2b.json`,
   `eval/results_real_company.json` → `first_runs`, `old_ledger_vs_ledger_room`; `eval/results.json` →
   `larger_model`; `eval/results_b70.json`.

**Shipping mode: strict reading.** Set `LEDGER_STRICT_READ=1` (or `pipeline.STRICT = True`); this is the recommended
mode, and every Ledger Room count above uses it. The code default is still off. The trade: the default also accepts
an invoice number or date that only one source supports, so fewer vouchers go to a person but more misreads get through; strict sends more to a person and lets fewer through. On the
same reads: real_tw default 7 sent, 3 let through, strict 14 sent, 0 let through; the 336-voucher box default 220
sent, 34 let through, strict 257 sent, 11 let through (9 of the 11 are utility bills, now routed to the platform
instead; see [Real data: one company's vouchers](real_data.md#real-data-one-companys-vouchers)). Design:
[strict.md](strict.md).

## Reproduce on the B70 host

Linux host with Intel Arc Pro B70 cards, from a clone of this repository. Each command writes the results file named
after it; compare it with the committed copy in `eval/`.

```bash
bash scripts/setup_host.sh            # check the GPUs, make .venv, install, telemetry off, download the models, run the tests
.venv/bin/python -m eval.bench_b70    # held-out set, 9B and 27B, fresh reads -> eval/results_b70.json
.venv/bin/python -m eval.bench_concurrency --set heldout --k 1,2,3,4 --devices GPU.0 --models-root ~/models
                                      # readers per card, power, memory -> eval/results_concurrency.json
.venv/bin/python -m eval.bench_concurrency --set heldout --k 2,4,6,8 --devices GPU.0,GPU.1 --models-root ~/models --out results_concurrency_2cards.json
.venv/bin/python -m eval.offline_check --model-dir ~/models/Qwen3.5-9B-int4-ov --device GPU.0 --out results_offline_b70.json
                                      # network watch during a live batch -> eval/results_offline_b70.json
LEDGER_ZOOM_MIN_H=0 .venv/bin/python -m eval.repro_oor --mode pipeline --device GPU.0 --reps 8 --out ~/oor_pipeline.json
                                      # the B70 reader error: full reads with the UBN zoom strip as it was (0 = no padding);
                                      # drop LEDGER_ZOOM_MIN_H for the padded default -> summary in eval/results_oor_repro.json
.venv/bin/python -m eval.bench_concurrency --sets heldout,hard,blind3,main --k 2 --devices GPU.0,GPU.1 --duration-min 70 --models-root ~/models --out results_longrun_zoompad.json
                                      # 70 minutes, two cards, strip padded to 448 px (the default) -> eval/results_longrun_zoompad.json
python3 eval/util_sampler.py 72 ~/util_longrun_zoompad.csv   # start beside the long run; then --summary -> results_longrun_zoompad_util.json
                                      # the run before the fix (results_longrun.json): same command with LEDGER_ZOOM_MIN_H=0
LEDGER_DEVICE=GPU.1 .venv/bin/python -m eval.agent_eval
                                      # the review agent: 9B + 27B on one card, held documents of the four sets, twice
                                      # -> eval/results_agent_b70.json
```

The key fields to compare: `results["Qwen3.5-9B-int4-ov"].ledger_room.micro.f1` and `.reading.fields_wrong`
(`results_b70.json`), `rows[].docs_per_hour` and `rows[].power.wh_per_1000_docs` (`results_concurrency*.json`), `pass`
(`results_offline_b70.json`). Each bench reads 47 documents; the concurrency runs take about 3 minutes per row.

## All B70 runs

Organizers' host: Intel Xeon 6787P, 251.5 GB RAM, two Intel Arc Pro B70 cards (`GPU.0`, `GPU.1`; the same GPU as the ASUS UGen B70), OpenVINO
2026.4.1, Linux. Reader Qwen3.5-9B INT4 (`OpenVINO/Qwen3.5-9B-int4-ov`), thinking mode off, fresh reads (cache off).
Every number below was copied from the results file named in its row; synthetic sets are synthetic. Rows up to the
offline check use reader r8 (code 3524145 to 77fa568). The two readers-per-card rows use reader r9 (2aead27), which
reads the held-out set the same way (0 fields wrong under both). Reader r9 changed reading after the first blind3
run; its blind3 and hard-set numbers are in the after-the-fix table below the notes.

| What was run | Result | Source (file → key) |
|---|---|---|
| **Held-out** (47 synthetic docs, layout B), 9B + Ledger Room, one card | micro F1 1.00 (24 of 24 planted issues, 0 false alarms); key fields wrong 0/319; 3.7 s per document (mean). Same 9B auditing on its own: F1 0.20 | `eval/results_b70.json` → `results["Qwen3.5-9B-int4-ov"]` (code 3524145) |
| **Hard set** (49 synthetic docs with capture damage, all clean), 9B | 10 of 283 key fields a person can read were read wrong (8 left blank, 2 document types); every document with a wrong field went to a person; 15 of 49 sent to a person (code 8a0fc91) | `eval/results_hard.json` → `readers["Qwen3.5-9B-int4-ov + OCR"].overall` |
| Hard set, same 49 docs, later code (77fa568) | 46/320 key fields wrong, 18 of 49 sent to a person. See the note under this table: the two rows count differently | `eval/results_tiered.json` → `sets.hard.first_only` |
| **blind3, first official run** (53 synthetic docs in six layouts the code had never seen; key frozen before any run), 9B alone, code 77fa568 | micro F1 0.836 (found 23, missed 4, 5 false alarms); key fields wrong 40/326 (buyer UBN 10/26, net 10/26, tax 7/26, total 10/53); 11 of 53 sent to a person; 1 clean document flagged. Of the 13 documents with a wrong key field, 11 went to a person; 2 hotel statements were accepted with the seller UBN read as the client's own and the buyer UBN left blank | `eval/results_tiered.json` → `sets.blind3.first_only`; the 13 / 11 / 2 split: `eval/blind3_first_run_per_doc.json` → `summary` (per document: wrong field names, whether it went to a person and why; no read values) |
| **Real photos, Taiwan** (41 receipt and e-invoice photos from Wikimedia Commons, CC BY-SA; answer labels written from the image only, frozen before runs; first run, code 99f36fc) | Ledger Room (9B): 13/164 fields wrong, 5 photos sent to a person, 5 photos with a wrong field not stopped. 9B on its own: 28/164 wrong, 20 photos with a wrong field and nothing stops them. OCR only: 29/164 wrong, 38 photos sent to a person | `eval/results_real_tw_b70.json` → `["Qwen3.5-9B-int4-ov"].conditions` |
| **Real photos, Taiwan, strict re-score** (the same 41 photos, saved 9B reads re-scored, code fa8c027; **after the fix**: the strict rule was written after seeing this set's misses, so this is not a first run) | Default reading at this code: 11/164 fields wrong, 7 photos sent to a person, 3 photos with a wrong field not stopped. Strict reading: 14 sent to a person, 0 not stopped. Strict with the 27B as second reader (12 photos escalated): 14 sent, 0 not stopped. 2B (code b6fd8bd): 18/164 wrong; default 10 sent, 6 not stopped; strict 21 sent, 1 not stopped | `eval/results_real_tw_strict.json`, `eval/results_real_tw_strict_2b.json` → `conditions` |
| **Real photos, Taiwan, second blind set real_tw2** (17 other Commons photos, labels written from the image only, key frozen before any read; first official run, code 968f9e0) | 9B: 15/66 fields wrong; default 8 sent to a person, 0 not stopped; strict 9 sent, 0 not stopped; strict with the 27B as second reader (9 escalated) 14/66 wrong, 9 sent, 0 not stopped. 2B (code b6fd8bd): 19/66 wrong; default 10 sent, 1 not stopped; strict 14 sent, 0 not stopped | `eval/results_real_tw2_strict.json`, `eval/results_real_tw2_strict_2b.json` → `conditions` |
| **Real vouchers, one company's petty-cash box** (336 vouchers; data rule and details in [Real data: one company's vouchers](real_data.md#real-data-one-companys-vouchers)) | 9B, strict: 257 sent to a person, 11 with a wrong field not stopped (9 of them utility bills); on the 113 invoices compared with the company's old ledger, 10 misread, all held | `eval/results_real_company.json` |
| **Real receipts, CORD v2** (193 receipts, validation + test splits, CC BY 4.0; total only, against the dataset's own ground truth; Taiwanese rules and Ledger Room's own reader not run) | 9B on its own: 3 of 193 totals wrong, none stopped. OCR + 9B, a total accepted only when two printed sources agree: 1 wrong and not stopped, 65 sent to a person (63 of those had been read right) | `eval/results_real_intl_cord_b70.json` → `["Qwen3.5-9B-int4-ov"].conditions` |
| **Two cards at once**, one 9B per card, started together, each card reading the same 47 held-out docs | 1,006 + 1,002 documents per hour (whole batch, wall clock); F1 1.00 and 0/319 fields wrong on both cards; no model reloads | `eval/results_dualcard_gpu0.json`, `eval/results_dualcard_gpu1.json` → `sets.heldout.first_only` (code 77fa568) |
| **27B as second reader** on the second card (the 9B reads everything, the 27B re-reads what the 9B is unsure of) | No gain. Hard set: 14 documents went to the 27B and 5 fields were taken from it, but still 18 of 49 to a person and 46/320 fields wrong, the same as the 9B alone. The unsure fields on that set are damaged on the image itself (covered, faded, written over), so a bigger reader has nothing more to read. Held-out: the 9B was unsure of no document. With the 27B as the only reader on held-out: F1 1.00, 0/319, 5.2 s per document against 3.7 s for the 9B | `eval/results_tiered.json` → `sets.hard.tiered` vs `sets.hard.first_only`; `eval/results_b70.json` → `results["Qwen3.5-27B-int4-ov"]` |
| **GPU driver error and workaround** | The first two blind3 attempts (code 7c3497b) stopped at document 8 of 53 with a GPU driver error (`CL_OUT_OF_RESOURCES`), before any scoring. Since then the bench reloads the 9B every 5 documents (`--first-restart-every 5`, about 9 s a reload). Counting the reloads, one card read 599 to 659 documents an hour (blind3 599, held-out 640, hard 651); leaving reload time out, 856 to 968. Replaced since by a fresh reader only after an error (after-the-fix table); cause found and fixed on 2026-11-07 (rows "Where the GPU error comes from" and "Long run, after the fix" below) | `eval/results_tiered.json` → `sets.blind3.note`, `sets.*.first_only` / `sets.*.tiered`: `docs_per_hour`, `docs_per_hour_excluding_first_reloads`, `first_restarts` |
| **Readers per card**, one B70, 1 to 4 copies of the 9B reading one queue of the 47 held-out docs (code 34f5246) | 1 reader: 1,034/h; 2 readers: 1,013/h; 3 readers: 993/h; 4 readers: 976/h: more reader processes did not raise the rate (batching not tried). Card memory 6.59 GB per reader (6.63 GB peak with one, 25.41 GB with four). Card power while reading 193.59 W, idle 64.18 W; 187.25 Wh per 1,000 documents (sampled power × read wall clock; the card's energy counter did not move). Scoring the same at every count: F1 1.00, 0/319 fields wrong, 0 sent to a person; no crashes | `eval/results_concurrency.json` → `rows[]`: `docs_per_hour`, `memory`, `power`, `scoring` |
| **Readers per card, both B70s**, the same queue split over two cards | 1 per card: 1,991/h; 2 per card: 1,944/h; 3 per card: 1,896/h; 4 per card: 1,857/h. One reader per card: 188.59 + 181.77 W while reading, 186.04 Wh per 1,000 documents. Same scoring as one card; no crashes. GPU.1 was not empty: before our readers loaded, another process on the shared host already held 14.86 GB of it (12.68 GB before the 4-per-card row), and with 3 and 4 readers per card GPU.1 ran full (31.89 GB), so the per-reader memory estimate there drops to 5.68 / 4.8 GB. The 2, 3, 4 per card rates may be held down by that; the one-card rows are not affected | `eval/results_concurrency_2cards.json` → `rows[]` |
| **Long run, both B70s, before the fix**, one 9B per card, the held-out, hard, blind3 and main sets in a loop for 70 minutes (code 48ebaf8; UBN zoom strip not padded) | 2,201 documents read in 70.0 min: 1,886/h, GPU.0 1,102 (944/h), GPU.1 1,099 (942/h). 28 reader restarts (24/h for the two cards; 12 on GPU.0, 16 on GPU.1), every one after a GPU plugin error (`CL_OUT_OF_RESOURCES`); 0 process crashes; reloading after restarts took 230.6 s, about 5% of the time. 3 of 2,201 documents (0.14%) were not read and went to a person (reason `reader_error`). Power 189.4 + 183.6 W while reading; 200.6 and 195.0 Wh per 1,000 documents (sampled watts × wall clock; the energy counter did not move). Scoring over time: held-out F1 1.00 in all 11 passes; blind3 17 or 18 key fields wrong in every pass (one document read differently between passes), 12 sent to a person every pass; the hard and main sets differ between passes only where a document was not read. The hard set has no planted issues, so its rule F1 is 0 by construction as soon as anything is flagged (read its 18 documents sent to a person, the same every pass, instead); main pass 5 scored F1 0.979 because one document was not read | `eval/results_longrun.json` → `rows[0]`: `docs_per_hour`, `per_device`, `restarts`, `docs_not_read_reader_failure`, `power`, `stability`, `passes[]` |
| **Compute engine busy**, both B70s, during the long run before the fix | GPU.0: busy 86% of the time (median 90%); GPU.1: 84% (median 89%). `xpu-smi` returns N/A for engine utilisation on this host, so `eval/util_sampler.py` read the Xe driver's DRM fdinfo every 5 s. Busy means the compute engine had work queued; it does not say how many execution units were active | `eval/results_longrun_util.json` → `cards`; raw samples `eval/longrun/util_longrun.csv` |
| **Where the GPU error comes from**: the 40 landscape photos around the long-run failures, 8 reads each per card (code c370a45) | The reader error comes from one image the reader sends the model: the UBN zoom strip, a crop about 852 × 140 px. Reading only the page: 0 of 640 reads failed. The full read (OCR and every model call): 4 of 640 failed, every one on that strip. The same full read with the strip padded with white to 448 px high: 0 of 640. 4 of 640 against 0 of 640 alone is suggestive (one-sided Fisher exact p about 0.06); the long run below is the larger test. Padding to 448 px is the default since a49cc19 (`LEDGER_ZOOM_MIN_H`, 0 turns it off). Not yet reported to OpenVINO | `eval/results_oor_repro.json` → `page_read_only`, `full_pipeline_before`, `full_pipeline_strip_padded_to_448`, `runs.*.error_calls` |
| **Long run, both B70s, after the fix**, the same four sets in a loop for 70 minutes, strip padded to 448 px (code c370a45 with `LEDGER_ZOOM_MIN_H=448`, the default from a49cc19) | 2,383 documents read in 70.0 min: 2,042/h, GPU.0 1,203 (1,031/h), GPU.1 1,180 (1,011/h). 0 reader restarts (28 before), 0 crashes, 0 documents sent to a person unread (3 before). Power 197.0 + 191.5 W while reading; 190.26 Wh per 1,000 documents (sampled watts × wall clock; the energy counter did not move). Every complete pass of every set scored the same: held-out F1 1.00, 0 key fields wrong, in 12 passes; blind3 18 key fields wrong and 12 of 53 sent to a person in 12 passes; main F1 1.00 in 11 passes; hard 45 key fields wrong and 18 of 49 sent to a person in 12 passes (rule F1 0 by construction, nothing planted) | `eval/results_longrun_zoompad.json` → `rows[0]`: `docs_per_hour`, `per_device`, `restarts`, `docs_not_read_reader_failure`, `power`, `stability` |
| **Compute engine busy**, both B70s, during the long run after the fix | GPU.0: busy 91% of the time (median 92%); GPU.1: 91% (median 92%), against 86% and 84% before the fix. Same method: Xe DRM fdinfo every 5 s | `eval/results_longrun_zoompad_util.json` → `cards`; raw samples `eval/longrun/util_longrun_zoompad.csv` |
| **Offline check** on the B70 | PASS: 47 documents read live, 175.8 s watched, 0 connections to a non-local address. The network adapter stayed up (a cloud host cannot unplug it), so this is not a cable-pulled test | `eval/results_offline_b70.json` |

**Hard set, why two rows.** Both runs are the 9B with reader r8 on the same 49 documents. `eval/hard_eval.py` (first
row) does not score the 37 key fields that the answer key marks as unreadable by a person, because the right outcome
there is to send the document to a person; `eval/read_accuracy.py`, used by `eval/bench_tiered.py` (second row),
scores every field. 36 of those 37 were read wrong or blank, so 10/283 and 46/320 are the same reading. The 3 extra
documents sent to a person come from a rule added between the two runs (8031031: a blank invoice number or seller UBN
on an invoice goes to a person); in the 8a0fc91 run, 3 of the 4 documents that should have gone to a person and did
not had a blank invoice number. For how well the reader reads, quote 10/283; for what the current code sends to a
person, quote 18 of 49.

**blind3 stays a first run.** These are the numbers of the first and only official blind3 run. Reader r9 (2aead27)
was changed afterwards because of what this run showed (an invoice number split by OCR taken for the buyer UBN,
amounts written one digit per box, a seller UBN equal to the client's own). Anything measured with r9 is labelled
"after the fix" and does not replace the rows above.

**Documents per hour, plainly.** Over 70 minutes on two B70s, one 9B reader per card read 2,042 documents an
hour: about 1,000 per card (1,031 and 1,011), with no restarts. That matches the 47-document batches (1,034 on one card,
1,991 on two), so the long run is the number to plan with. More reader processes on the same card did not raise the
rate (1,013, 993, 976 with 2, 3, 4). Each card's compute engine was busy 91% of the time: one reader keeps it busy
most of the time; batching inside one reader has not been tried. A card draws about 190 W while reading, about
0.19 kWh per 1,000 documents (sampled). The 32 GB on a B70 is headroom (the 9B needs about 6.59 GB, the 27B fits),
not a reason to stack readers.

The GPU driver error is fixed. Before the fix, the reader hit a GPU plugin error (`CL_OUT_OF_RESOURCES`) about every
5 minutes per card: the first 70-minute run had 28 restarts, 3 of 2,201 documents sent to a person unread, 1,886
documents an hour and the compute engines busy 86% and 84%. The error came from one image, the thin UBN zoom strip
(about 852 × 140 px); padding it to 448 px high gave 0 errors in 640 reads and 0 restarts in the 70-minute run after
the fix. Not yet reported to OpenVINO. The 599 to 659 an hour in the reload row above was the first workaround.

<!-- r9 after-fix rows go here -->

**After the fix: reader r9 (2aead27).** Not an independent test any more: r9 was changed because of what the first
blind3 run showed. The first-run rows above stay the headline.

| What was run | Result | Source (file → key) |
|---|---|---|
| Held-out, 47 docs, 9B alone | F1 1.00, 0/319 key fields wrong, 0 of 47 sent to a person | `eval/results_after_fix_r9.json` → `sets.heldout.first_only` |
| Hard set, 49 docs, 9B alone (first reader only; the 27B pass was not run, which is why the file says incomplete) | 45/320 key fields wrong, 18 of 49 sent to a person, 2 clean documents flagged | `eval/results_after_fix_r9.json` → `sets.hard.first_only` |
| blind3, 53 docs, 9B alone, restart-on-error bench (7d00d4d) | F1 1.00 (found 27, missed 0, 0 false alarms), 18/326 key fields wrong, 12 of 53 sent to a person, 0 clean documents flagged; 883 documents an hour; one reader error (`CL_OUT_OF_RESOURCES`) recovered by a fresh process, no fixed reloads. A first attempt (bench 34f5246) reused the broken process and was discarded | `eval/results_after_fix_r9_blind3.json` → `rows[0]` |

### Real photos: first run, then strict reading

On 41 real Taiwan receipt photos (real_tw, first run on the B70) the 9B on its own let 20 photos with a wrong field
through. Ledger Room with the default reading stopped 15 of them and sent 5 photos to a person; OCR with the same
rules let 1 through but sent 38 to a person. That was the trade of the default reading: 4 more wrong photos got
through than with OCR and the rules, and a person opened 5 photos instead of 38. The first-run row above stays the
record of that run.

Those misses led to strict reading ([strict.md](strict.md)). The same saved reads were
re-scored: the default reading at the newer code lets 3 through and sends 7 to a person; strict lets 0 through and
sends 14. The strict rule was written after seeing this set, so that is an after-the-fix number. The first blind test
of strict reading is real_tw2 (17 photos, key frozen before any read, first run): 0 let through and 9 sent to a person
with strict reading, 0 and 8 with the default. Answer labels on both public sets were written from the image only and
frozen before runs; no person checked them. Method: [real_photos.md](real_photos.md).
