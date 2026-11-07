r"""Extract the MOF issued invoice prefixes (統一發票字軌) from the MOF offline checking program's database.

The Ministry publishes the prefixes for each two-month period (字軌公告). Its offline checking program (營業稅離線審核,
installed under C:\ETAX\BLR) ships them in Firebird files; this reads the rows (period YYYMM, prefix, format code)
straight from the file bytes, without a Firebird client, and writes ledger_room/data/invoice_tracks.json.

    python -I scripts/extract_invoice_tracks.py C:\ETAX\BLR\DB
Only purchase-side formats are kept: 21 (triplicate), 22 (two-part / cash register), 25 (triplicate cash register
and e-invoices)."""
import glob
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROW = re.compile(rb"\x05\x00(\d{5})\xf1\x00\x04\x02\x00([A-Z]{2})\xfa\x00\x03\x01\x00([A-Z])\xfd\x00\x04\x02\x00(\d\d)")
KEEP = ("21", "22", "25")


def main(db_dir: str) -> None:
    out = defaultdict(lambda: defaultdict(set))
    for f in sorted(glob.glob(str(Path(db_dir) / "**" / "*.FDB"), recursive=True)):
        for m in ROW.finditer(Path(f).read_bytes()):
            per, pre, _, fmt = (x.decode() for x in m.groups())
            if fmt in KEEP:
                out[per][fmt].add(pre)
    data = {"source": "MOF 統一發票字軌 as shipped in the MOF offline checking program (營業稅離線審核 11500.01)",
            "formats": {"21": "triplicate", "22": "two-part / cash register", "25": "triplicate cash register, e-invoice"},
            "periods": {p: {f: sorted(v) for f, v in sorted(d.items())} for p, d in sorted(out.items())}}
    dst = Path(__file__).resolve().parents[1] / "ledger_room" / "data" / "invoice_tracks.json"
    dst.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(dst, len(data["periods"]), "months")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"C:\ETAX\BLR\DB")
