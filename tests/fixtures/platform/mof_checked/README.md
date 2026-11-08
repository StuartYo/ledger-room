# Files that passed the MOF offline checking program

Checked on 2026-11-07 with the Ministry of Finance offline checking program (營業稅離線審核, version 11500.01),
period 115-09/10, synthetic client 67094646 (form 401). Every value here is synthetic.

- `67094646.TXT`: the golden media file (`../golden/`) with each invoice prefix replaced by one the Ministry issued
  for that period and format (formats 21/23/31/33 → EC, 25/35 → ED; 22 kept EF). The checker matches prefixes
  against its issued-prefix table, so the made-up prefixes in `../golden/` are flagged (10 lines) and these are not.
  Nothing else differs.
  (Correction 2026-11-08: the copy first run on 11-07 also had line 7's deduction code changed from 2 to 1 by mistake,
  so its TXT and TET_U disagreed on boxes 32–35 while each passed the checker on its own. `eval/filing_check.py` caught
  it. Line 7 is back to 2 and the pair was run through the checker again on 2026-11-08: step 1 0 errors, step 2 table
  produced, step 3 1 company, 0 with errors. `test_mof_checked_pair_is_consistent_and_differs_from_golden_only_in_prefixes`
  keeps the pair consistent.)
- `67094646.TET_U`: the return data file written by `filing.file_return` with the filer as `self_or_agent="1"`.

Checker result: step 1 (media file) 1 company, 0 with errors; step 2 statistics table produced; step 3 (return data)
1 company, 0 with errors. The first run of step 3 rejected box 106 on a 401; see the README section
"MOF checking program".
