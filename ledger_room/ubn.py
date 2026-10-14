"""Taiwan Unified Business Number (統一編號, UBN) checksum.

Rule source: Ministry of Finance Fiscal Information Agency,
"營利事業統一編號檢查碼邏輯修正說明" (published 110-12-22):
https://www.fia.gov.tw/singlehtml/3?cntId=c4d9cff38c8642ef8872774ee9987283
The check changed from "weighted sum divisible by 10" to "divisible by 5";
checking programs had to be updated by 112-03-31 (2023-03-31).

Algorithm (both rules):
  weights 1,2,1,2,1,2,4,1 over all 8 digits; every product is reduced to the
  sum of its decimal digits (e.g. 2*6=12 -> 1+2=3); S = sum of those.
  new rule: S % 5 == 0, or (7th digit == '7' and (S + 1) % 5 == 0)
  old rule: S % 10 == 0, or (7th digit == '7' and (S + 1) % 10 == 0)
The 7th-digit case exists because 7*4 = 28 -> 2+8 = 10, which may also be
read as 1+0 = 1 (i.e. S - 9, which is congruent to S + 1).
Cross-checked against python-stdnum (commit 5fd7498, 2026-07) examples:
00501503 valid, 00501502 invalid, 12345675 valid (7th-digit case).
"""
from __future__ import annotations

from dataclasses import dataclass

WEIGHTS = (1, 2, 1, 2, 1, 2, 4, 1)
RULE_URL = "https://www.fia.gov.tw/singlehtml/3?cntId=c4d9cff38c8642ef8872774ee9987283"


def weighted_sum(ubn: str) -> int:
    if len(ubn) != 8 or not ubn.isdigit():
        raise ValueError(f"UBN must be 8 digits, got {ubn!r}")
    total = 0
    for digit, w in zip(ubn, WEIGHTS):
        p = int(digit) * w
        total += p // 10 + p % 10
    return total


def _valid(ubn: str, mod: int) -> bool:
    s = weighted_sum(ubn)
    if s % mod == 0:
        return True
    return ubn[6] == "7" and (s + 1) % mod == 0


def is_valid_new(ubn: str) -> bool:
    """2023 rule (current): divisible by 5."""
    return _valid(ubn, 5)


def is_valid_old(ubn: str) -> bool:
    """Pre-2023 rule: divisible by 10. Legacy software still uses this."""
    return _valid(ubn, 10)


@dataclass(frozen=True)
class UbnCheck:
    ubn: str
    well_formed: bool
    valid_new: bool
    valid_old: bool
    weighted_sum: int | None
    seventh_digit_case: bool

    @property
    def explanation(self) -> str:
        if not self.well_formed:
            return f"'{self.ubn}' is not 8 digits"
        s = self.weighted_sum
        base = f"weighted sum S={s}"
        if self.seventh_digit_case:
            base += " (7th digit is 7, so S+1 also accepted)"
        if self.valid_new and not self.valid_old:
            return base + "; passes the 2023 mod-5 rule, would FAIL the old mod-10 rule (legacy software false alarm)"
        if self.valid_new:
            return base + "; passes the 2023 mod-5 rule"
        return base + "; fails the 2023 mod-5 rule"


def check(ubn: str | None) -> UbnCheck:
    raw = (ubn or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        return UbnCheck(raw, False, False, False, None, False)
    s = weighted_sum(raw)
    return UbnCheck(
        ubn=raw,
        well_formed=True,
        valid_new=is_valid_new(raw),
        valid_old=is_valid_old(raw),
        weighted_sum=s,
        seventh_digit_case=raw[6] == "7",
    )


def make_valid(prefix7: str, rule: str = "new") -> list[str]:
    """All check digits d such that prefix7+d is valid (used by the synthetic generator)."""
    fn = is_valid_new if rule == "new" else is_valid_old
    return [prefix7 + str(d) for d in range(10) if fn(prefix7 + str(d))]
