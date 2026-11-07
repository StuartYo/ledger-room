"""Synthetic firm period for the period check (SYNTHETIC: every company, UBN, invoice number and amount is made up).

python -m synth.period_check            writes data/period_synthetic/ and eval/expected_period_check_synthetic.json

One firm, four clients, the September-October 2026 period. For each client:
  platform/<UBN>/115-09_10/<UBN>.txt   the e-invoice platform's media filing download (81-character records)
  platform/<UBN>/115-10/<UBN>.txt      (client P04 only) a second, monthly download that overlaps the first
  platform/<UBN>/carrier_115-09_10.csv (clients P01, P03) a carrier export with item names
  filed/<UBN>/<period>/<UBN>.TXT       media files the firm already filed for earlier periods
  paper.json                           the paper vouchers' fields, as if read correctly (reading is measured elsewhere)

Planted errors (the key says which voucher carries which, and what should happen to it):
  prior_filing_einvoice  a platform line whose invoice number and seller are in an earlier filed media file
  prior_filing_paper     a paper voucher (dated before the period: a deferred claim) already in an earlier filing
  amount_diff            a paper photo and the platform line of the same invoice disagree on the total
  cross_client           an invoice on one client's platform download also photographed in another client's box
  s19_einvoice           a platform line whose carrier export item is a gift or a staff trip (VAT Act Art. 19)
  s19_to_confirm         a platform line whose carrier item is alcohol (to confirm: deductible if for resale)
  record_conflict        one invoice twice in the client's downloads with different amounts
  fixed_asset            a platform line with a net amount of NT$80,000 or more (Audit Regulations Art. 77-1)
  void_paper             a paper photo of an invoice the platform marks voided
Decoys that must NOT be flagged: a paper photo of a platform invoice with the same total; an earlier filing with the
same invoice number from another seller a year earlier (invoice tracks are reused); a byte-identical re-download;
allowances; carrier items that are ordinary office purchases; voided platform lines (left out, not errors).

The key is written by this generator from what it planted, before the checker runs, and is not edited afterwards.
"""
from __future__ import annotations

import csv
import io
import json
import random
from pathlib import Path

from ledger_room import ubn as ubnlib
from ledger_room.export import media_record

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "period_synthetic"
KEY = ROOT / "eval" / "expected_period_check_synthetic.json"
SEED = 20261107
PERIOD = ["2026-09-01", "2026-10-31"]

CARRIER_HEADER = ["載具自訂名稱", "發票日期", "發票號碼", "發票金額", "發票狀態", "折讓", "賣方統一編號", "賣方名稱",
                  "賣方地址", "買方統編", "消費明細_數量", "消費明細_單價", "消費明細_金額", "消費明細_品名"]
OFFICE_ITEMS = ["影印紙A4", "原子筆", "碳粉匣", "文件夾", "便利貼", "釘書針", "資料夾", "白板筆", "延長線", "收納箱"]


class Gen:
    def __init__(self, seed: int):
        self.r = random.Random(seed)
        self.used_no: set[str] = set()
        self.used_ubn: set[str] = set()

    def ubn(self) -> str:
        while True:
            v = f"{self.r.randrange(10_000_000, 99_999_999):08d}"
            if v not in self.used_ubn and ubnlib.check(v).valid_new:
                self.used_ubn.add(v)
                return v

    def inv_no(self) -> str:
        while True:
            v = "".join(self.r.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(2)) + f"{self.r.randrange(10**8):08d}"
            if v not in self.used_no:
                self.used_no.add(v)
                return v

    def date(self, months=("09", "10"), year=2026) -> str:
        return f"{year}-{self.r.choice(months)}-{self.r.randrange(1, 29):02d}"

    def net(self, lo=200, hi=60_000) -> int:
        return self.r.randrange(lo, hi)


def tax_of(net: int) -> int:
    return int(net * 0.05 + 0.5)


def main() -> dict:
    g = Gen(SEED)
    sellers = [g.ubn() for _ in range(40)]
    clients = []
    for cid, name, n_clean in (("P01", "合成甲商行有限公司（虛構）", 30), ("P02", "合成乙設計有限公司（虛構）", 36),
                               ("P03", "合成丙餐飲有限公司（虛構）", 24), ("P04", "合成丁工程有限公司（虛構）", 28)):
        clients.append({"id": cid, "name": name, "ubn": g.ubn(), "tax_reg_no": f"{g.r.randrange(10**9):09d}",
                        "period": PERIOD, "policy": {}, "n_clean": n_clean})
    by_id = {c["id"]: c for c in clients}

    key: list[dict] = []
    # per client: current download records, extra download records, filed records, carrier invoices, paper docs
    plat = {c["id"]: [] for c in clients}
    plat_extra = {c["id"]: [] for c in clients}
    filed = {c["id"]: {} for c in clients}          # period folder -> records
    carrier = {c["id"]: [] for c in clients}
    paper = []

    def add_key(vid, cid, side, planted, rules_, outcome):
        key.append({"voucher": vid, "client_id": cid, "side": side, "planted": planted,
                    "expected_rules": rules_, "expected_outcome": outcome})

    def einv(cid, *, no=None, seller=None, date=None, net=None, fmt="25", planted=(), rules_=(), outcome="ready",
             keyed=True):
        c = by_id[cid]
        no, seller = no or g.inv_no(), seller or g.r.choice(sellers)
        date, net = date or g.date(), net if net is not None else g.net()
        rec = dict(fmt=fmt, buyer=c["ubn"], seller=seller, no=no, date=date, net=net, tax=tax_of(net), tax_type="1")
        plat[cid].append(rec)
        if keyed:
            add_key(f"{cid}/platform/{no}", cid, "e-invoice", list(planted), list(rules_), outcome)
        return rec

    def paper_doc(cid, n, *, no=None, seller=None, date=None, net=None, buyer="client", doc_type="triplicate_invoice",
                  items=None, planted=(), rules_=(), outcome="ready"):
        c = by_id[cid]
        no = no or g.inv_no()
        seller = seller or g.r.choice(sellers)
        date = date or g.date()
        net = net if net is not None else g.net(100, 20_000)
        b = c["ubn"] if buyer == "client" else buyer
        tax = tax_of(net) if b else None
        doc_id = f"{cid}/{date[:7]}/p{n:02d}.jpg"
        f = {"doc_type": doc_type, "invoice_no": no, "date": date, "seller_name": f"賣方{seller[-3:]}（虛構）",
             "seller_ubn": seller, "buyer_name": c["name"] if b == c["ubn"] else None, "buyer_ubn": b,
             "items": items or [{"desc": g.r.choice(OFFICE_ITEMS), "qty": 1, "amount": net + (tax or 0)}],
             "net": net if b else None, "tax": tax, "total": net + (tax or 0)}
        paper.append({"doc_id": doc_id, "client_id": cid, "month": date[:7], "fields": f})
        add_key(doc_id, cid, "paper", list(planted), list(rules_), outcome)
        return f

    # ---- clean platform lines, a few format 21 (三聯式), the rest 25 (electronic)
    for c in clients:
        for _ in range(c["n_clean"]):
            einv(c["id"], fmt=g.r.choice(["25", "25", "25", "21"]))
        # a voided input line on every download: left out, not an error
        no = g.inv_no()
        plat[c["id"]].append(dict(fmt="25", buyer=c["ubn"], seller=g.r.choice(sellers), no=no, date=g.date(), net=0,
                                  tax=0, tax_type="F"))
        add_key(f"{c['id']}/platform/{no}/void", c["id"], "e-invoice", [], [], "left_out")
        # earlier filed periods: ordinary lines
        filed[c["id"]]["115-07_08"] = [dict(fmt="25", buyer=c["ubn"], seller=g.r.choice(sellers), no=g.inv_no(),
                                            date=g.date(("07", "08")), net=g.net(), tax_type="1", deduction="1")
                                       for _ in range(20)]
        for r in filed[c["id"]]["115-07_08"]:
            r["tax"] = tax_of(r["net"])
        # a few sales lines so the download looks like the real one (ignored by the input checks)
        for _ in range(5):
            n = g.net(1_000, 90_000)
            plat[c["id"]].append(dict(fmt="35", buyer=g.r.choice(sellers), seller=c["ubn"], no=g.inv_no(),
                                      date=g.date(), net=n, tax=tax_of(n), tax_type="1"))

    # ---- planted on the e-invoice side
    for cid in ("P01", "P01", "P03"):             # prior_filing_einvoice: filed early by mistake last period
        r = einv(cid, date=g.date(("09",)), planted=["prior_filing_einvoice"], rules_=["DUP_PRIOR_FILING"],
                 outcome="held")
        filed[cid]["115-07_08"].append(dict(r, deduction="1"))
    # decoy: same invoice number, another seller, filed a year earlier (tracks are reused)
    r = einv("P01")
    filed["P01"]["114-09_10"] = [dict(r, seller=next(s for s in sellers if s != r["seller"]), date="2025-09-15",
                                      deduction="1")]
    for cid, kw in (("P01", "精緻禮盒"), ("P01", "員工旅遊團費"), ("P03", "威士忌")):
        r = einv(cid, net=g.net(1_000, 20_000), planted=["s19_to_confirm" if kw == "威士忌" else "s19_einvoice"],
                 rules_=["S19_NONDEDUCTIBLE"])
        carrier[cid].append((r, [kw]))
    for cid in ("P01", "P01", "P03", "P03", "P03"):   # carrier items that are ordinary purchases
        r = einv(cid, net=g.net(300, 9_000))
        carrier[cid].append((r, g.r.sample(OFFICE_ITEMS, 2)))
    for cid in ("P02", "P04"):                        # fixed_asset
        einv(cid, net=g.r.randrange(85_000, 240_000), planted=["fixed_asset"], rules_=["FIXED_ASSET_HINT"])
    # record_conflict: a monthly re-download after the seller corrected the amount
    r = einv("P04", date="2026-10-06", planted=["record_conflict"], rules_=["PLATFORM_RECORD_CONFLICT"],
             outcome="held")
    plat_extra["P04"].append(dict(r, net=r["net"] + 900, tax=tax_of(r["net"] + 900)))
    # decoy: an identical October line in both downloads (de-duplicated, not a conflict)
    r = einv("P04", date="2026-10-18")
    plat_extra["P04"].append(dict(r))
    # allowances: one on a current invoice, one on a filed July invoice (both normal)
    for cid, orig in (("P02", plat["P02"][0]), ("P03", filed["P03"]["115-07_08"][0])):
        plat[cid].append(dict(fmt="23", buyer=by_id[cid]["ubn"], seller=orig["seller"], no=orig["no"],
                              date="2026-10-20", net=500, tax=25, tax_type="1"))
        add_key(f"{cid}/platform/{orig['no']}/allowance", cid, "e-invoice", [], [], "ready")

    # ---- paper boxes
    n = {c["id"]: 0 for c in clients}

    def nxt(cid):
        n[cid] += 1
        return n[cid]

    for c in clients:
        for _ in range(6):                            # clean triplicate invoices to the client
            paper_doc(c["id"], nxt(c["id"]))
        for _ in range(2):                            # consumer-style e-invoices without buyer UBN: expense only
            paper_doc(c["id"], nxt(c["id"]), buyer=None, doc_type="e_invoice")
    # decoy: photo of a platform invoice with the same total -> booked from the platform, not an error
    for cid in ("P01", "P02", "P04"):
        r = next(x for x in plat[cid] if x["fmt"] == "25" and x["tax_type"] == "1" and x["buyer"] == by_id[cid]["ubn"])
        paper_doc(cid, nxt(cid), no=r["no"], seller=r["seller"], date=r["date"], net=r["net"], doc_type="e_invoice",
                  outcome="on_platform")
    # amount_diff: same number, the paper total differs (digits swapped / a later corrected invoice)
    for cid in ("P01", "P02", "P03"):
        r = einv(cid, net=g.net(2_000, 30_000), planted=["amount_diff"], rules_=["PLATFORM_AMOUNT_DIFF"],
                 outcome="held")
        paper_doc(cid, nxt(cid), no=r["no"], seller=r["seller"], date=r["date"], net=r["net"] + g.r.choice([90, 900, -90]),
                  doc_type="e_invoice", outcome="on_platform")
    # prior_filing_paper: dated in August (deferred claim), already in the July-August filing
    for cid in ("P02", "P04"):
        f = paper_doc(cid, nxt(cid), date=g.date(("08",)), planted=["prior_filing_paper"], rules_=["DUP_PRIOR_FILING"],
                      outcome="held")
        filed[cid]["115-07_08"].append(dict(fmt="21", buyer=by_id[cid]["ubn"], seller=f["seller_ubn"],
                                            no=f["invoice_no"], date=f["date"], net=f["net"], tax=f["tax"],
                                            tax_type="1", deduction="1"))
    # cross_client: on one client's platform download, photographed in another client's box
    for owner, other, buyer in (("P01", "P02", None), ("P03", "P04", "owner"), ("P04", "P01", None)):
        r = einv(owner, net=g.net(500, 15_000))
        paper_doc(other, nxt(other), no=r["no"], seller=r["seller"], date=r["date"], net=r["net"],
                  buyer=by_id[owner]["ubn"] if buyer == "owner" else None, doc_type="e_invoice",
                  planted=["cross_client"], rules_=["DUP_CROSS_CLIENT"], outcome="held")
    # void_paper: the platform marks it voided (existing rule VOID_ON_PLATFORM)
    no, seller = g.inv_no(), g.r.choice(sellers)
    plat["P02"].append(dict(fmt="25", buyer=by_id["P02"]["ubn"], seller=seller, no=no, date="2026-09-12", net=0, tax=0,
                            tax_type="F"))
    add_key(f"P02/platform/{no}/void", "P02", "e-invoice", [], [], "left_out")
    paper_doc("P02", nxt("P02"), no=no, seller=seller, date="2026-09-12", doc_type="e_invoice",
              planted=["void_paper"], rules_=["VOID_ON_PLATFORM"], outcome="held")

    # ---- write
    def lines(cid, recs):
        c = by_id[cid]
        out = []
        for i, r in enumerate(recs, 1):
            if r["fmt"].startswith("3"):
                y, m = r["date"][:4], r["date"][5:7]
                rec = (r["fmt"] + c["tax_reg_no"] + f"{i:07d}" + f"{int(y) - 1911:03d}{m}" + r["buyer"] + r["seller"]
                       + r["no"] + f"{r['net']:012d}" + r["tax_type"] + f"{r['tax']:010d}" + " " * 9)
            else:
                rec = media_record(fmt=r["fmt"], tax_reg_no=c["tax_reg_no"], serial=i, date=r["date"],
                                   buyer_ubn=r["buyer"], seller_ubn=r["seller"], voucher_no=r["no"], amount=r["net"],
                                   tax=r["tax"], deduction=r.get("deduction", "1"), tax_type=r["tax_type"])
            assert len(rec) == 81, rec
            out.append(rec)
        return "\r\n".join(out) + "\r\n"

    if OUT.exists():
        import shutil
        shutil.rmtree(OUT)
    for c in clients:
        cid, u = c["id"], c["ubn"]
        recs = sorted(plat[cid], key=lambda r: (r["fmt"], r["date"], r["no"]))
        p = OUT / "platform" / u / "115-09_10" / f"{u}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(lines(cid, recs).encode("ascii"))
        if plat_extra[cid]:
            p = OUT / "platform" / u / "115-10" / f"{u}.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(lines(cid, plat_extra[cid]).encode("ascii"))
        for per, recs in filed[cid].items():
            p = OUT / "filed" / u / per / f"{u}.TXT"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(lines(cid, sorted(recs, key=lambda r: (r["fmt"], r["date"], r["no"]))).encode("ascii"))
        if carrier[cid]:
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\r\n")
            w.writerow(CARRIER_HEADER)
            for r, items in carrier[cid]:
                total = r["net"] + r["tax"]
                parts = [total // len(items)] * len(items)
                parts[-1] += total - sum(parts)
                for desc, amt in zip(items, parts):
                    w.writerow(["公司載具", r["date"].replace("-", ""), r["no"], amt, "已確認", "否", r["seller"],
                                f"賣方{r['seller'][-3:]}（虛構）", "", c["ubn"], 1, amt, amt, desc])
            for _ in range(2):                         # consumer invoices on the carrier, no buyer UBN: not inputs
                amt = g.net(60, 900)
                w.writerow(["公司載具", g.date().replace("-", ""), g.inv_no(), amt, "已確認", "否", g.r.choice(sellers),
                            "賣方（虛構）", "", "", 1, amt, amt, "咖啡"])
            w.writerow([])
            w.writerow(["註：本檔為合成資料（synthetic），所有公司、統編、發票號碼皆為虛構。"])
            (OUT / "platform" / u / "carrier_115-09_10.csv").write_bytes(buf.getvalue().encode("utf-8-sig"))
    firm = {"synthetic": True,
            "note": "SYNTHETIC. All companies, UBNs, invoice numbers and amounts generated by synth/period_check.py.",
            "seed": SEED, "period": PERIOD,
            "clients": [{k: v for k, v in c.items() if k != "n_clean"} for c in clients]}
    (OUT / "firm.json").write_text(json.dumps(firm, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "paper.json").write_text(json.dumps({"synthetic": True, "note": "fields as if read correctly",
                                                "docs": paper}, ensure_ascii=False, indent=1), encoding="utf-8")
    planted: dict[str, int] = {}
    for k in key:
        for p in k["planted"]:
            planted[p] = planted.get(p, 0) + 1
    out = {"synthetic": True,
           "note": "Answer key written by synth/period_check.py from what it planted, before the checker was run. "
                   "Frozen: not edited after the first run.",
           "seed": SEED, "planted": planted,
           "outcomes": {o: sum(1 for k in key if k["expected_outcome"] == o)
                        for o in ("ready", "held", "on_platform", "left_out")},
           "vouchers": key}
    KEY.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    k = main()
    print(json.dumps({"planted": k["planted"], "outcomes": k["outcomes"], "vouchers": len(k["vouchers"])}, indent=1))
