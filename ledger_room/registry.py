"""Optional local lookup in the MOF business-registration open data (全國營業(稅籍)登記資料集).

Why: a checksum alone cannot tell an OCR misread from a made-up number. With a local copy of the register,
Ledger Room can say "one digit away from registered business X" (likely a misread) or "passes the checksum but
is not registered" (new or closed business, or a fake number).

Offline by design: the firm downloads BGMOPEN1.zip once from data.gov.tw (dataset 9400, published daily by the MOF
Fiscal Information Agency; it lists businesses currently operating) and points LEDGER_REGISTRY at the .zip or the
.csv inside it. Nothing is looked up online at run time.

    $env:LEDGER_REGISTRY = "D:\\registry\\BGMOPEN1.zip"

The loader only needs two columns, found by header name: 統一編號 (UBN) and 營業人名稱 (business name). Both names
are listed as main fields on the dataset page (checked 2026-10-14); the loader has not yet been run on a downloaded
copy, and it stops with an error if no 統一編號 column is found.
The evaluation never loads a register: the synthetic UBNs are random numbers.
"""
from __future__ import annotations

import csv
import io
import os
from datetime import datetime
from pathlib import Path

from . import ubn as ubnlib

UBN_COLS = ("統一編號",)
NAME_COLS = ("營業人名稱", "名稱")


class Registry:
    def __init__(self, names: dict[str, str], label: str):
        self.names = names
        self.label = label

    def __len__(self) -> int:
        return len(self.names)

    def name(self, ubn: str) -> str | None:
        return self.names.get(ubn)

    def near_misses(self, ubn: str) -> list[tuple[str, str]]:
        """Registered UBNs that differ from `ubn` in exactly one digit and pass the 2023 checksum."""
        out = []
        for i in range(8):
            for dgt in "0123456789":
                if dgt == ubn[i]:
                    continue
                cand = ubn[:i] + dgt + ubn[i + 1:]
                n = self.names.get(cand)
                if n is not None and ubnlib.is_valid_new(cand):
                    out.append((cand, n))
        return out

    @classmethod
    def from_csv(cls, path: str | Path) -> "Registry":
        path = Path(path)
        if path.suffix.lower() == ".zip":
            import zipfile
            with zipfile.ZipFile(path) as z:
                inner = next(n for n in z.namelist() if n.lower().endswith(".csv"))
                raw = z.read(inner)
        else:
            raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp950", "big5"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError(f"cannot decode {path}")
        rows = csv.reader(io.StringIO(text))
        header, iu, iname = None, None, None
        names: dict[str, str] = {}
        for row in rows:
            if header is None:
                cells = [c.strip() for c in row]
                if any(c in UBN_COLS for c in cells):
                    header = cells
                    iu = next(i for i, c in enumerate(cells) if c in UBN_COLS)
                    iname = next((i for i, c in enumerate(cells) if c in NAME_COLS), None)
                continue
            if len(row) <= iu:
                continue
            u = row[iu].strip()
            if len(u) == 8 and u.isdigit():
                names[u] = row[iname].strip() if iname is not None and len(row) > iname else ""
        if header is None:
            raise ValueError(f"{path}: no header row with a 統一編號 column")
        mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
        return cls(names, f"{path.name}, copy of {mtime}, {len(names):,} businesses")


def load_default() -> Registry | None:
    """The register named by LEDGER_REGISTRY, or None when the firm has not set one up."""
    p = os.environ.get("LEDGER_REGISTRY")
    if not p:
        return None
    return Registry.from_csv(p)
