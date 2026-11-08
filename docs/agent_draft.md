# Review agent: what it may do and how code checks it (frozen 2026-11-09)

Written before the agent was wired into the product, from the experiment of 2026-11-08/09 (Intel Cloud host, second
Arc Pro B70, synthetic sets only). Do not change the check below to clear more documents; change it only to refuse
more, and say so here.

## Flow

1. The usual check runs: OCR + the 9B reader, reconciliation, the rules.
2. Every document held for a person (severity hold or review) with a reading reason (`READ_REVIEW`,
   `AMOUNT_MISMATCH`, `BUYER_PRESUMED`) goes to the agent: the 27B vision model, temperature 0 (greedy), one tool call
   per turn, at most 6 calls, then it must finish.
3. Tools: `zoom` (crop and enlarge a region; code runs OCR on the crop), `calc` (net + tax = total, tax = 5% of net),
   `ubn_check` (check digit), `history` (seller UBNs confirmed on the firm's other documents; the same invoice number
   elsewhere), `platform` (this client's e-invoice platform download, when there is one), `finish` (proposed values,
   what it saw, the fields it leaves to a person).
4. Code checks every proposed value (below). The rules run again with the accepted values. A document with no hold or
   review left is `agent-verified`: it does not wait for a person. Anything else goes to a person as before.
5. Everything is kept: the original reasons, each tool call and its result, each proposal, each check and the outcome
   (result.json `docs[].agent`, and an `agent_check` entry in the run's signed audit log).
6. A person can reject the agent's fixes on a document (the document is back to its first reading and its reasons)
   or change a value by hand; both are logged in the run's review log.

## The check (v3)

A proposed value is accepted only when it has the right format (UBN: 8 digits and a valid check digit; invoice number:
2 letters + 8 digits; date: a real date; amounts: whole numbers) and at least one source that is not a model:

| Field | Accepted source |
|---|---|
| invoice number, UBN, date | the OCR of the agent's zoom crop reads the same string; or the page OCR has it and the first reader (9B) said the same |
| seller UBN | also: confirmed on the firm's other documents from the same seller (history), or the platform download |
| amounts | net + tax = total and tax = 5% of net, **and the other two numbers** were confirmed on the first reading or are printed on the page (OCR) |
| total of a receipt without net and tax | it sits on an OCR line with a total label (總計, 合計, 應付 ...) |

Refused in any case: a UBN that OCR places with the other party; a UBN equal to the other party's; amounts that do not
add up after the fix.

**"The digits appear on the page" is never evidence for an amount.** Net, tax and total are all numbers on the page:
the first version (v1) accepted a net (6440) as the total that way (hard set, W04 / IMG_4741, answer 6762).

## Not decided by the agent

The four decisions (confirm, non-deductible, exclude, defer) stay with a person. The agent only proposes readings.
