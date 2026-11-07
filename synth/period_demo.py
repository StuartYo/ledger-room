"""Demo platform data for the `heldout` inbox (SYNTHETIC: every UBN, invoice number and amount is made up).

python -m synth.period_demo     writes data/platform/<UBN>/115-09_10/ and data/filed/<UBN>/115-07_08/ for H01-H05

The web app loads these for every run (pipeline.audit(platform="auto")), so the batch screen shows the whole period:
the clients' e-invoices from a (synthetic) platform download next to the paper the model read. The evaluation does
not load them, so no evaluated number changes. Planted, so the period check has something to show:
  H02  the platform's copy of photographed invoice CB71368445 has another total      -> PLATFORM_AMOUNT_DIFF
  H05  photographed invoice LE39243585 (dated 2026-08-30) is in the July-August filing -> DUP_PRIOR_FILING (paper)
  H03  one platform e-invoice was already filed in July-August                         -> DUP_PRIOR_FILING
  H01  a carrier export names a gift box on one platform e-invoice                     -> Art. 19 (code 3)
  H04  one platform e-invoice of NT$128,000 net                                        -> FIXED_ASSET_HINT
The two photographed invoice numbers are what the reader printed for those (synthetic) scans; nothing was tuned.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from ledger_room import ubn as ubnlib
from ledger_room.export import media_record

ROOT = Path(__file__).resolve().parents[1]
SEED = 20261108


def main() -> None:
    r = random.Random(SEED)
    clients = {c["id"]: c for c in json.loads((ROOT / "data" / "heldout" / "clients.json")
                                              .read_text(encoding="utf-8"))["clients"]}
    used = set()

    def ubn():
        while True:
            v = f"{r.randrange(10_000_000, 99_999_999):08d}"
            if v not in used and ubnlib.check(v).valid_new:
                used.add(v)
                return v

    def no():
        while True:
            v = "".join(r.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(2)) + f"{r.randrange(10**8):08d}"
            if v not in used:
                used.add(v)
                return v

    sellers = [ubn() for _ in range(25)]
    taxreg = {cid: f"{r.randrange(10**9):09d}" for cid in clients}

    def line(cid, fmt, seller, inv, date, net, tax_type="1", serial=1):
        tax = 0 if tax_type == "F" else int(net * 0.05 + 0.5)
        return media_record(fmt=fmt, tax_reg_no=taxreg[cid], serial=serial, date=date, buyer_ubn=clients[cid]["ubn"],
                            seller_ubn=seller, voucher_no=inv, amount=0 if tax_type == "F" else net, tax=tax,
                            deduction="1", tax_type=tax_type)

    cur = {cid: [] for cid in clients}
    old = {cid: [] for cid in clients}
    for cid in clients:
        for _ in range(r.randrange(16, 26)):
            cur[cid].append(("25", r.choice(sellers), no(), f"2026-{r.choice(['09', '10'])}-{r.randrange(1, 29):02d}",
                             r.randrange(150, 40_000), "1"))
        cur[cid].append(("25", r.choice(sellers), no(), "2026-10-09", 0, "F"))          # voided: left out
        for _ in range(12):
            old[cid].append(("25", r.choice(sellers), no(), f"2026-{r.choice(['07', '08'])}-{r.randrange(1, 29):02d}",
                             r.randrange(150, 40_000), "1"))
    # H02: the platform copy of the photographed CB71368445 (paper NT$1,796) carries NT$1,691
    cur["H02"].append(("25", "67205273", "CB71368445", "2026-10-10", 1610, "1"))
    # H05: the photographed LE39243585 (2026-08-30) was filed with July-August
    old["H05"].append(("25", "40859942", "LE39243585", "2026-08-30", 514, "1"))
    # H03: a September e-invoice that was keyed into the July-August filing by mistake
    x = ("25", r.choice(sellers), no(), "2026-09-01", 6_800, "1")
    cur["H03"].append(x)
    old["H03"].append(x)
    # H04: equipment
    cur["H04"].append(("25", r.choice(sellers), no(), "2026-10-15", 128_000, "1"))
    # H01: carrier export with item names for three platform lines, one of them a gift box
    gift = ("25", r.choice(sellers), no(), "2026-10-20", 3_000, "1")
    cur["H01"].append(gift)
    carrier = [(gift, "中秋禮盒")] + [(cur["H01"][i], d) for i, d in ((0, "影印紙A4"), (1, "碳粉匣"))]

    for cid, c in clients.items():
        u = c["ubn"]
        for base, recs in ((ROOT / "data" / "platform" / u / "115-09_10", cur[cid]),
                           (ROOT / "data" / "filed" / u / "115-07_08", old[cid])):
            base.mkdir(parents=True, exist_ok=True)
            txt = "\r\n".join(line(cid, *x, serial=i) for i, x in enumerate(sorted(recs, key=lambda x: (x[3], x[2])), 1))
            (base / f"{u}.{'txt' if 'platform' in base.parts else 'TXT'}").write_bytes((txt + "\r\n").encode("ascii"))
    rows = ["發票日期,發票號碼,發票金額,發票狀態,折讓,賣方統一編號,賣方名稱,買方統編,消費明細_數量,消費明細_單價,消費明細_金額,消費明細_品名"]
    for (fmt, seller, inv, date, net, _), desc in carrier:
        tot = net + int(net * 0.05 + 0.5)
        rows.append(f"{date.replace('-', '')},{inv},{tot},已確認,否,{seller},賣方（虛構）,{clients['H01']['ubn']},1,{tot},{tot},{desc}")
    rows.append("註：合成資料（synthetic），所有統編、發票號碼、金額皆為虛構。")
    (ROOT / "data" / "platform" / clients["H01"]["ubn"] / "carrier_115-09_10.csv").write_bytes(
        ("\r\n".join(rows) + "\r\n").encode("utf-8-sig"))


if __name__ == "__main__":
    main()
