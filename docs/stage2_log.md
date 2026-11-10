# Stage II development log

The filing output and voucher list below have not been run on the B70 host. The B70 measurements are under
[b70_measurements.md](b70_measurements.md).

**Filing output.** `ledger_room/filing.py` (`file_return`) writes, for one client and one two-month period,
`<UBN>.TXT` (the full media file: the platform download's sales and input lines plus Ledger Room's pre-checked input
lines, serials renumbered), `<UBN>.TET_U` (the return data file for form 401, or form 403 with the non-deductible
ratio) and a totals JSON. Form 404, special-rate sales, purchases of foreign services and the direct-deduction method
are not supported and raise an error. `eval/filing_check.py` checks a TXT and TET_U pair on its own (the TET_U layout
typed in again from Attachment 6, the boxes recomputed from the lines); it is not the MOF checking program.
`tests/fixtures/platform/golden/` holds the pair for a synthetic client and the September–October 2026 period.

**MOF checking program (2026-11-07).** We ran the synthetic client's 401 pair through the Ministry's offline
checking program (營業稅離線審核, version 11500.01) on Windows:

| Step | First run | After the fix |
|---|---|---|
| 1. Input and sales lines (`.TXT`, 13 lines) | 10 lines flagged, all on the invoice letter prefix | 0 errors |
| 2. Statistics table | — | produced |
| 3. Return data (`.TET_U`) | rejected: "box 106 must be 0 on a 401" | 0 errors |

- The prefix flags came from the test data: the checker matches each prefix against the Ministry's issued prefixes
  for that period and format, and our fixtures use made-up ones. With the period's issued prefixes swapped in, every
  line passed. Field layout, lengths, tax amounts and tax codes were not flagged.
- Box 106 was a real bug: boxes 103–106 belong to form 403 only. On a 401, box 106 is now 0 and the tax due is
  computed from box 101 (`filing.return_totals`, `eval/filing_check.py`, test
  `test_401_box_106_is_zero_and_tax_reg_fifth_digit_checked`).
- The checker also rejects a 稅籍編號 whose 5th digit is not 0–4; `filing.tet_u_fields` now checks this first.
- The files that passed are in `tests/fixtures/platform/mof_checked/`.
- Form 403 and the zero-rate (T02) file were not run through the checker. Ledger Room writes no T02 file.

**Input lines only.** `python -m ledger_room.export data/out/runs/<run id> --tax-reg taxreg.json`
writes one `<client UBN>.TXT` per client: 81-character ASCII input records, laid out from the MOF regulation
[營業稅電子資料申報繳稅作業要點](https://law-out.mof.gov.tw/LawContent.aspx?id=GL009478) (amended 2024-04-12, 台財資字第1130001073號令):
point 17 (ASCII), point 18(1) (81 characters a record, file name = UBN + .TXT), point 20 (field meanings and codes)
and the position table in
[Attachment 5](https://law-out.mof.gov.tw/Download.ashx?FileID=46520&id=GL009478&type=LAW).

- Only `READY` lines are written. `HOLD` and `NOT_IN_FILE` lines stay out, and a line that cannot be written (no
  date, amount or invoice number) is listed with the reason.
- The 9-digit tax registration number (稅籍編號) is assigned by the tax office and is not the UBN, so the firm has to
  supply it (`--tax-reg`, a JSON map of client UBN to number). Without it the export stops with an error.
- `eval/media_file_check.py` checks a written file on its own: the layout is typed in again from Attachment 5 and
  does not import the writer. It checks length, character type, code ranges, duplicate serial numbers, UBN checksums
  and the file name. `tests/test_media_txt.py` compares records with ones assembled by hand from Attachment 5.

Not verified yet:

- Only the 401 pair has been through the MOF checking program, on synthetic data; form 403 and real client data
  have not.
- The e-invoice platform import has only read synthetic downloads written from the published layout; no real
  download from the platform has been parsed yet. The period check has only run on a synthetic firm period whose
  errors were planted by the rule author.
- No accounting software has imported them, and no bookkeeper has compared lines with the paper vouchers.
- The file number field of the TET_U is written as a placeholder (00000000), CRLF separates records, and text
  fields are padded with spaces on the right. The regulation does not spell these out; the checking program
  accepted all three on the 401 run.
- Ledger Room writes no monthly summary records (formats 26 / 27) or allowances (23 / 24) from photos; such lines in
  the platform download are kept as downloaded. Customs-collected VAT certificates (28) are recognised and always
  held: the bookkeeper keys the certificate number and tax. Deduction codes 2 and 4 (fixed assets) are set by the
  bookkeeper after a `FIXED_ASSET_HINT`.

**Voucher list for accounting software.** `ledger_room/voucher_export.py` writes one row per input voucher in 13
columns (date, invoice number, format code, seller UBN, seller name, net, tax, total, deduction code, tax type,
summary, status, suggested account) as .xlsx, or as CSV in UTF-8 with a BOM. The status is pre-checked, hold, or
platform-unchecked (a platform line: exact numbers, no item names); a photographed voucher already in the platform
download appears once, as the platform row. The suggested account, from the MOEA chart of accounts, is filled only
when a keyword in the rules' own tables matched, and is marked as a suggestion. We found no published fixed import
template for 正航, 鼎新 A1 or 文中, so the file needs a column mapping in the software. It has not been imported into
any accounting software yet; how much keying it saves is still to be measured.

**B70 measurement script.** `python -m eval.bench_b70` is ready for the B70 host: device inventory, model download
after a free-space check (the pre-converted `OpenVINO/Qwen3.5-9B-int4-ov` and `OpenVINO/Qwen3.5-27B-int4-ov`, both
Apache-2.0), then Ledger Room and the pure-model baseline on the held-out set with fresh reads, judged against the
targets frozen in the table above (`tests/test_bench_offline.py` fails if that table changes). It writes
`eval/results_b70.json`. `scripts/setup_host.sh` (Linux) and `scripts/setup_host.ps1` (Windows) install it. It ran
on the B70 host on 2026-11-06 (code 3524145); the results are under
[b70_measurements.md](b70_measurements.md). GPU memory was read on Linux from
`/proc/<pid>/fdinfo`: peak 7.99 GB of card memory for the 9B and 17.04 GB for the 27B
(`results[...].peak_memory.gpu_resident_vram0_gb`). `eval/bench_tiered.py` runs the two-card measurements
(`eval/results_tiered.json`, `eval/results_dualcard_gpu0.json` / `gpu1.json`). The laptop dry runs
(`eval/results_b70_dryrun.json`, `eval/results_tiered_dryrun.json`) only check that the scripts work; their timings are
not results.

### Page splitting

**Page splitting (a separate tool, `ledger_room/split.py`).** A firm scans A4 pages with several vouchers taped on each. `ledger_room/split.py` (`split_page`, or `python -m ledger_room.split page.jpg [out_dir]`) returns one box per voucher with classical image processing on the CPU, about 0.03 s a page: the print is grouped into blobs, and each blob is cut again and again along the widest near-blank band that leaves both sides voucher sized; a printed rule across the band blocks the cut, a paper edge along it does not. It does not turn crops upright. On the real company box ([data rule](real_data.md#real-data-one-companys-vouchers); 107 pages, 336 boxes drawn by hand) every threshold was set on 72 pages, and the other 35 (every third page by sorted file name) were run once after the method was frozen: 84 of 109 vouchers found at IoU 0.5 or more (69 at 0.7 or more), 25 missed, 20 extra boxes, 24 of 35 pages fully right. On the 72 tuning pages: 171 of 227 (151 at 0.7), 49 extra boxes, 47 pages fully right. It fails on touching vouchers with no blank band between them (e-invoice strips side by side, stacked postage receipts), on small receipts in a dense grid (0 of 13 two-part register receipts on the held-out pages) and on sideways bills, which it cuts in two (`eval/split_eval.py`, `eval/results_split.json`).
