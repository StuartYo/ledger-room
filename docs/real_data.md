# Real data: one company's vouchers

**Data rule.** One small Taiwanese company's petty-cash box, used with the owner's consent. Only aggregate counts are
published: no images, no company name, no tax ID, no per-voucher values. The images, the answer key and the
per-voucher files stay outside git. Every other mention of "the real company box" in this repository refers to this set
and this rule.

The box holds 336 vouchers: 83 e-invoices, 24 triplicate invoices, 14 two-part cash-register invoices, 155 utility
bills, 34 postage receipts, 14 tax bills and 12 receipts. Answer key: 4 labelers read the page images only (no ledger,
no OCR, no Ledger Room output), frozen (sha256 prefix fcff2ca60a393f7d) before any Ledger Room read; it was not made
or checked by a bookkeeper. Fields scored: invoice number, date, seller UBN, total, net, tax (a utility bill's printed
invoice number is the previous period's e-invoice). First runs on one B70 (`GPU.0`):

| First run, 336 vouchers | 9B (code fa8c027) | 2B (code b6fd8bd) |
|---|---|---|
| Seconds a photo on the B70 | 3.87 | 2.45 |
| Share of fields wrong: OCR only / model alone / Ledger Room | 60% / 27% / 21.5% | 60% / 68.1% / 36.1% |
| Default reading: sent to a person / wrong and not held | 220 / 34 | 262 / 34 |
| Strict reading: sent to a person / wrong and not held | 257 / 11 | 309 / 7 |
| Strict, the 121 invoices only: sent to a person / with a wrong field / wrong and not held | 56 / 52 / 1 | 98 / 79 / 4 |

Strict counts were re-scored from the same reads (`eval/strict_rescore.py`). With strict reading the 2B lets fewer
vouchers through over the whole box (7 against 11) because it sends 52 more to a person; on the invoices it lets more
through (4 against 1). Of the 9B's 11 strict misses, 9 are utility bills, 1 a tax bill and 1 a triplicate invoice
(`by_kind_9B`). The 27B as second reader on the 9B's strict run: 164 photos escalated, 5.96 s each, 241 sent to a
person, still 11 not held, share of fields wrong 18.2%.

**Against the company's old ledger.** The company's own ledger rows for its invoices were typed by a general cloud chat
AI and checked once by the owner. 113 of the 121 invoices could be paired with a ledger row (106 by exact invoice
number, 7 by a near-miss number with the same date or amount), compared on the three fields that ledger keeps:
invoice number, date, total.

| Same 113 invoices | Wrong | Wrong and not held | Sent to a person |
|---|---|---|---|
| Old ledger (chat AI, checked once) | 15 rows (invoice number 7, date 6, total 3) | 15 (nobody held them) | n/a |
| Ledger Room 9B, strict | 10 | 0 | 53 |
| Ledger Room 9B, default | 10 | 0 | 52 |
| Ledger Room 2B, strict | 36 | 1 | 91 |
| Ledger Room 2B, default | 36 | 13 | 52 |

Not a person-versus-machine race: the old ledger is one way a small company actually kept its books. On all six
scored fields the 9B let 1 of the 113 through (a seller tax ID), which the old ledger does not record.

**After the fix: utility bills routed to the platform.** The utility-bill keywords (commit 1d7a07c) were written after
seeing this box, so these are after-the-fix numbers on the same saved reads, no model run. A recognised utility bill is
not booked from paper: its input tax comes from the utility's e-invoice on the MOF platform, and the bill is kept as
proof of payment. It is neither held nor exported. Recognised: 155 of 155, 0 false (OCR text only;
`eval/results_utility_detect.json`). 9B strict: 112 sent to a person (257 before), 2 wrong and not held outside the
bills (11 before, 9 of them bills); default 109 and 2. 2B strict: 157 and 4; default 114 and 27.

All counts: `eval/results_real_company.json` (`first_runs`, `by_kind_9B`, `invoices_121`, `old_ledger_vs_ledger_room`,
`after_fix_utility_routing`). Page splitting on the same box: [stage2_log.md](stage2_log.md#page-splitting).
