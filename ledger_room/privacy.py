"""Personal-data handling: display masking, purge when a client's contract ends, and an encryption-at-rest self-check.

Rules this answers (bookkeepers' personal-data safeguard regulation, 記帳士及記帳及報稅代理業務人個人資料檔案安全維護計畫及
業務終止後個人資料處理作業辦法, https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=G0340165, and 記帳士法 Art. 17):
  * Art. 15: permissions differ by person          -> roles (web/users.py); only UNMASK_ROLES may unmask, and each
                                                      unmask is written to the operation log with the reason.
  * Art. 17: display masking (顯示隱碼)             -> mask_result / mask_text / mask_csv; the web app masks by default.
  * Art. 19: usage and trail records kept >= 5 years; after the business ends, record how the data was destroyed
                                                   -> purge() deletes a client's working copies, keeps the audit logs,
                                                      and writes the deletion record (what, when, who, method, hashes)
                                                      to the operation log (same HMAC chain as the audit log).
  * encryption at rest: there is no dependency-free file cipher in the standard library, so Ledger Room relies on the
    operating system's full-disk encryption (BitLocker / LUKS / FileVault) and warns at start-up when the data folder
    is on a volume that is not encrypted. It never refuses to start.

python -m ledger_room.privacy purge --client C01 --set main --reason "contract ended 2026-10-31" --dry-run
python -m ledger_room.privacy purge --client C01 --set main --reason "contract ended 2026-10-31" --operator amy
python -m ledger_room.privacy check-encryption [--path data]
See docs/data_handling.md.
"""
from __future__ import annotations

import argparse
import copy
import csv
import getpass
import hashlib
import io
import json
import ntpath
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"          # same folder as pipeline.DATA (not imported, so this module loads without the reader)

SAFEGUARD_URL = "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=G0340165"
ART19_URL = "https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=G0340165&flno=19"

# ----------------------------------------------------------------------------------------------------------------
# 1. Masking (顯示隱碼)
# ----------------------------------------------------------------------------------------------------------------

# Roles allowed to see personal data unmasked, after giving a reason that goes to the operation log. Staff check
# vouchers against the receipt image, so they do not need the masked text fields in clear.
UNMASK_ROLES = frozenset(r.strip() for r in os.environ.get("LEDGER_UNMASK_ROLES", "admin").split(",") if r.strip())

# What counts as personal data in Ledger Room's outputs, field by field. "kept" fields are not personal data.
FIELD_POLICY = {
    "seller_name": "masked when it reads as a natural person's name (no company or shop word in it)",
    "buyer_name": "masked when it reads as a natural person's name (a person buying with a receipt)",
    "note": "free text: phone numbers, national ID / resident certificate numbers, e-mail addresses and "
            "labelled names (負責人 / 姓名 / 聯絡人 ...) are masked",
    "items[].desc": "free text, as note",
    "exceptions[].message, evidence[].value": "free text, as note; names found in the name fields are masked too",
    "clients[].owner / responsible_person / representative / contact / phone / email / id_no / home_address":
        "masked (the owner or contact of a client is a natural person)",
    "clients[].name": "masked only when the client is marked sole_proprietor / natural_person and the name reads "
                      "as a person's name",
    "seller_ubn, buyer_ubn, client ubn": "kept: a company's 統一編號 is not personal data",
    "invoice_no, date, amounts, tax, category, doc_id": "kept",
    "receipt images and evidence crops": "not masked: they are the voucher itself, which the bookkeeper must see",
}

NAME_FIELDS = ("seller_name", "buyer_name")
CLIENT_PERSONAL_FIELDS = ("owner", "responsible_person", "representative", "contact", "contact_name", "phone",
                          "mobile", "email", "id_no", "national_id", "home_address")
# string values under these keys are identifiers or hashes, never personal data; masking must not touch them
SAFE_KEYS = frozenset({"doc_id", "image", "crop", "related_docs", "client_id", "id", "rule_id", "url", "run_id",
                       "sha256", "hash", "prev", "mac", "audit_log", "export_media", "seller_ubn", "buyer_ubn",
                       "ubn", "client_ubn", "invoice_no", "provenance", "event", "ts", "set", "inbox", "path",
                       "kind", "sha256_before", "sha256_after", "rules", "citations", "field", "evidence_fields",
                       "severity", "doc_type", "category", "model", "device", "basis", "user", "role"})

CJK = "㐀-䶿一-鿿豈-﫿"
_CJK_RUN = re.compile(f"^[{CJK}]+$")
# a name containing one of these is a business, a shop or an institution
ORG_WORDS = ("公司", "企業", "商行", "行號", "商號", "工作室", "事務所", "商店", "協會", "學會", "基金會", "合作社",
             "醫院", "診所", "學校", "大學", "銀行", "集團", "有限", "股份", "控股", "藥局", "超商", "便利", "餐廳",
             "飯店", "酒店", "旅館", "旅行社", "市場", "百貨", "小吃", "麵館", "書局", "文具", "五金", "中心", "政府",
             "國稅局", "加油站", "停車場", "商旅", "超市", "量販", "食堂", "餐館", "小館")
# words the reader returns in a name field that are labels or placeholders, not names
NOT_NAMES = {"買受人", "買方", "賣方", "賣受人", "未提供", "不詳", "無", "客戶", "顧客", "本人", "個人", "消費者",
             "現金", "收據", "發票", "統一發票", "營業人", "店家", "商家", "公司", "台端", "貴公司"}
# ... and a 2-4 character name ending in one of these is a shop (王記, 阿明店). Characters common in given names
# (行, 軒, 堂, 園) are left out on purpose: when in doubt, mask.
ORG_SUFFIX = "店社坊館廠所號記屋舖鋪棧站場局處署院部會廳肆"
LATIN_ORG = {"co", "co.", "ltd", "ltd.", "inc", "inc.", "llc", "corp", "corp.", "company", "store", "shop", "studio",
             "cafe", "café", "restaurant", "hotel", "bank", "market", "mart", "group", "trading", "enterprise",
             "pte", "bhd", "gmbh", "sa", "ag", "plc", "the", "and", "&", "warung", "toko", "pt", "cv"}
TITLES = ("先生", "小姐", "女士", "太太")

_NB = "(?<![0-9A-Za-z])"        # not preceded by a letter or digit (Python's \\w would include CJK)
_NA = "(?![0-9A-Za-z])"
MOBILE_RE = re.compile(_NB + r"(?:\+?886[-\s]?9\d{2}|09\d{2})[-\s]?\d{3}[-\s]?\d{3}" + _NA)
LANDLINE_RE = re.compile(_NB + r"(?:\(0\d{1,2}\)|0\d{1,2})[-\s]?\d{3,4}[-\s]?\d{4}" + _NA)   # >= 9 digits: never a UBN
# national ID (A123456789) and the 2021 resident certificate number (A800000000 / A900000000). The old resident
# number (two letters + 8 digits) has the same shape as an invoice number (AB12345678), so it is not matched.
ID_RE = re.compile(_NB + r"[A-Z][1289]\d{8}" + _NA)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LABELLED_NAME_RE = re.compile(rf"(負責人|代表人|姓名|聯絡人|收件人|經手人|承辦人|買受人|客戶)(\s*[:：]?\s*)([{CJK}]{{2,4}})(?![{CJK}])")


def can_unmask(role: str | None) -> bool:
    return role in UNMASK_ROLES


def is_personal_name(name) -> bool:
    """True when `name` reads as a natural person's name rather than a business. Errs towards True (masking is
    reversible by an unmask, a leak is not)."""
    if not isinstance(name, str):
        return False
    s = name.strip()
    compact = re.sub(r"[\s·・．]", "", s)
    if compact and _CJK_RUN.match(compact):
        s = compact
    if not s or any(ch.isdigit() for ch in s):
        return False
    if _CJK_RUN.match(s):
        if s in NOT_NAMES or any(w in s for w in ORG_WORDS):
            return False
        if s.endswith(TITLES):
            return True
        return 2 <= len(s) <= 4 and s[-1] not in ORG_SUFFIX
    words = s.split()
    if not 2 <= len(words) <= 4:
        return False
    if any(w.lower() in LATIN_ORG for w in words):
        return False
    return all(re.fullmatch(r"[A-Z][a-zA-Z'\-]*\.?", w) for w in words)


def mask_name(name: str) -> str:
    """王小明 -> 王○明, 王明 -> 王○, 歐陽小明 -> 歐○○明, 王先生 -> 王先生, John Smith -> J*** S***."""
    s = name.strip()
    if _CJK_RUN.match(re.sub(r"\s", "", s) or "x"):
        s = re.sub(r"\s", "", s)
        for t in TITLES:
            if s.endswith(t):
                return s[0] + "○" * (len(s) - 1 - len(t)) + t
        if len(s) <= 2:
            return s[0] + "○" * (len(s) - 1)
        return s[0] + "○" * (len(s) - 2) + s[-1]
    return " ".join(w[0] + "***" for w in s.split())


def _mask_digits(m: re.Match, keep_head: int = 4, keep_tail: int = 3) -> str:
    s = m.group(0)
    n = sum(ch.isdigit() for ch in s)
    out, k = [], 0
    for ch in s:
        if ch.isdigit():
            out.append(ch if k < keep_head or k >= n - keep_tail else "*")
            k += 1
        else:
            out.append(ch)
    return "".join(out)


def mask_id(s: str) -> str:
    """A123456789 -> A12****789"""
    return s[:3] + "*" * (len(s) - 6) + s[-3:]


def mask_email(s: str) -> str:
    local, _, dom = s.partition("@")
    return local[:1] + "***@" + dom


def mask_text(s: str, names=()) -> str:
    """Mask the personal data found in a free-text string. `names`: personal names already known (from the name
    fields of the same result), replaced wherever they appear."""
    if not isinstance(s, str) or not s:
        return s
    for n in sorted({n for n in names if n}, key=len, reverse=True):
        if n in s:
            s = s.replace(n, mask_name(n))
    s = LABELLED_NAME_RE.sub(lambda m: m.group(1) + m.group(2) + (mask_name(m.group(3)) if is_personal_name(m.group(3))
                                                                  else m.group(3)), s)
    s = EMAIL_RE.sub(lambda m: mask_email(m.group(0)), s)
    s = ID_RE.sub(lambda m: mask_id(m.group(0)), s)
    s = MOBILE_RE.sub(_mask_digits, s)
    s = LANDLINE_RE.sub(_mask_digits, s)
    return s


def personal_names(result: dict) -> set[str]:
    """Every personal name in the name fields of a result (docs and evidence) and in the client list."""
    found = set()
    for d in result.get("docs") or []:
        for k in NAME_FIELDS:
            v = (d.get("fields") or {}).get(k)
            if is_personal_name(v):
                found.add(v.strip())
    for e in result.get("exceptions") or []:
        for ev in e.get("evidence") or []:
            if ev.get("field") in NAME_FIELDS and is_personal_name(ev.get("value")):
                found.add(ev["value"].strip())
    for c in result.get("clients") or []:
        found |= _client_names(c)
    return found


def _client_names(c: dict) -> set[str]:
    out = set()
    for k in ("owner", "responsible_person", "representative", "contact", "contact_name"):
        if is_personal_name(c.get(k)):
            out.add(c[k].strip())
    if _natural_person_client(c) and is_personal_name(c.get("name")):
        out.add(c["name"].strip())
    return out


def _natural_person_client(c: dict) -> bool:
    return bool(c.get("sole_proprietor") or c.get("natural_person")
                or c.get("type") in ("sole_proprietor", "natural_person", "獨資", "個人"))


def mask_client(c: dict, names=()) -> dict:
    c = dict(c)
    names = set(names) | _client_names(c)
    for k in CLIENT_PERSONAL_FIELDS:
        v = c.get(k)
        if not isinstance(v, str) or not v:
            continue
        if k in ("id_no", "national_id"):
            c[k] = mask_id(v) if len(v) >= 7 else "***"
        elif k == "home_address":
            c[k] = _mask_address(v)
        elif is_personal_name(v):
            c[k] = mask_name(v)
        else:
            c[k] = mask_text(v, names)
    if _natural_person_client(c) and is_personal_name(c.get("name")):
        c["name"] = mask_name(c["name"])
    return c


def _mask_address(a: str) -> str:
    """Keep the city and district, hide the rest: 臺北市大安區虛構路12號 -> 臺北市大安區****"""
    m = re.match(rf"^([{CJK}]{{2,3}}[市縣])?([{CJK}]{{1,3}}[區鄉鎮市])?", a)
    head = "".join(g for g in (m.groups() if m else ()) if g)
    return head + "****" if head else "****"


def _walk(obj, names, key=None):
    if isinstance(obj, dict):
        return {k: (obj[k] if k in SAFE_KEYS else _walk(obj[k], names, k)) for k in obj}
    if isinstance(obj, list):
        return [_walk(x, names, key) for x in obj]
    if isinstance(obj, str):
        if key in NAME_FIELDS and is_personal_name(obj):
            return mask_name(obj)
        return mask_text(obj, names)
    return obj


def mask_result(result: dict) -> dict:
    """A copy of a run's result.json with personal data masked; UBNs, invoice numbers, amounts and ids untouched."""
    names = personal_names(result)
    out = _walk(copy.deepcopy({k: v for k, v in result.items() if k not in ("clients", "rules")}), names)
    if "rules" in result:
        out["rules"] = result["rules"]
    if "clients" in result:
        out["clients"] = [mask_client(c, names) for c in result["clients"]]
    out["masked"] = True
    return out


def mask_entries(entries: list[dict], names=()) -> list[dict]:
    """Audit-log / operation-log entries for display."""
    return [_walk(e, set(names)) for e in entries]


def mask_csv(text: str, names=()) -> str:
    """The media-file CSV export with free-text cells masked (UBN, invoice and amount columns are kept)."""
    rd = csv.reader(io.StringIO(text.lstrip("﻿")))
    rows = list(rd)
    if not rows:
        return text
    head = rows[0]
    safe = {i for i, h in enumerate(head)
            if h in SAFE_KEYS or h.startswith(("client_ubn", "seller_ubn", "buyer_ubn", "invoice_no", "doc_id",
                                               "client_id"))}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(head)
    for r in rows[1:]:
        w.writerow([c if i in safe else mask_text(c, names) for i, c in enumerate(r)])
    return ("﻿" if text.startswith("﻿") else "") + buf.getvalue()


# ----------------------------------------------------------------------------------------------------------------
# 2. Purge when a client's contract ends (業務終止後個人資料處理)
# ----------------------------------------------------------------------------------------------------------------

METHOD = ("file deleted through the file system (os.remove); derived files rewritten without the client's rows. "
          "No overwrite pass: on SSDs it is not reliable. The freed blocks are unreadable only if the volume is "
          "encrypted (BitLocker / LUKS), which docs/data_handling.md makes a deployment requirement.")
KEPT_RULE = ("kept: audit logs and the operation log are usage records / trail data that Art. 19 of the safeguard "
             "regulation requires for at least five years; they hold document ids, SHA-256 hashes and events, not "
             "receipt contents")
IMG_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff", ".bmp", ".pdf"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class PurgeError(Exception):
    pass


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _rel(p: Path, base: Path) -> str:
    try:
        return p.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def find_sets(client_id: str, data_dir: Path) -> list[str]:
    hits = []
    for cj in sorted(data_dir.glob("*/clients.json")):
        if cj.parent.name == "out":
            continue
        try:
            ids = {c.get("id") for c in json.loads(cj.read_text(encoding="utf-8")).get("clients", [])}
        except (ValueError, OSError):
            ids = set()
        if client_id in ids or (cj.parent / "clients" / client_id).is_dir():
            hits.append(cj.parent.name)
    return hits


def _rewrite_result(path: Path, set_name: str, cid: str):
    """(new bytes or None when nothing to change, removed counts, crop files referenced by removed exceptions)"""
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None, {}, []
    prefix = f"{set_name}/clients/{cid}/"
    rset = (r.get("summary") or {}).get("set")
    def own(x: dict) -> bool:
        return str(x.get("image", "")).startswith(prefix) or (rset == set_name and x.get("client_id") == cid)

    def own_client(c: dict) -> bool:
        return rset == set_name and c.get("id") == cid

    docs = r.get("docs") or []
    exs = r.get("exceptions") or []
    d_rm = [d for d in docs if own(d)]
    e_rm = [e for e in exs if own(e)]
    c_rm = [c for c in (r.get("clients") or []) if own_client(c)]
    if not (d_rm or e_rm or c_rm):
        return None, {}, []
    crops = [ev["crop"] for e in e_rm for ev in (e.get("evidence") or []) if ev.get("crop")]
    r["docs"] = [d for d in docs if not own(d)]
    r["exceptions"] = [e for e in exs if not own(e)]
    if "clients" in r:
        r["clients"] = [c for c in r["clients"] if not own_client(c)]
    s = r.get("summary")
    if isinstance(s, dict):
        s["n_docs"] = len(r["docs"])
        s["n_exceptions"] = len(r["exceptions"])
        if isinstance(s.get("by_client"), dict):
            s["by_client"].pop(cid, None)
        if isinstance(s.get("by_rule"), dict):
            s["by_rule"] = {k: sum(1 for e in r["exceptions"] if e.get("rule_id") == k) for k in s["by_rule"]}
    r.setdefault("purged_clients", []).append(cid)
    return (json.dumps(r, ensure_ascii=False, indent=1).encode("utf-8"),
            {"docs": len(d_rm), "exceptions": len(e_rm), "clients": len(c_rm)}, crops)


def _rewrite_csv(path: Path, cid: str, doc_ids: set[str], same_set: bool):
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError):
        return None, 0
    rows = list(csv.DictReader(io.StringIO(raw)))
    if not rows or "client_id" not in rows[0]:
        return None, 0
    keep = [x for x in rows if not ((same_set and x.get("client_id") == cid) or x.get("doc_id") in doc_ids)]
    if len(keep) == len(rows):
        return None, 0
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\r\n")
    w.writeheader()
    w.writerows(keep)
    return ("﻿" + buf.getvalue()).encode("utf-8"), len(rows) - len(keep)


def _rewrite_clients(path: Path, cid: str):
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cl = cfg.get("clients", [])
    gone = [c for c in cl if c.get("id") == cid]
    if not gone:
        return None, None
    cfg["clients"] = [c for c in cl if c.get("id") != cid]
    return json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8"), gone[0]


def plan_purge(client_id: str, set_name: str | None = None, data_dir: Path | str = DATA) -> dict:
    """Everything a purge would delete, rewrite, keep, or leave for a person to review. Writes nothing."""
    data_dir = Path(data_dir)
    out = data_dir / "out"
    if not _ID_RE.match(client_id or "") or (set_name is not None and not _ID_RE.match(set_name)):
        raise PurgeError("client and inbox ids are letters, digits, '-', '_' and '.' only")
    hits = find_sets(client_id, data_dir)
    if set_name is None:
        if not hits:
            raise PurgeError(f"client {client_id} is not in any inbox under {data_dir}")
        if len(hits) > 1:
            raise PurgeError(f"client {client_id} is in several inboxes ({', '.join(hits)}); pass --set")
        set_name = hits[0]
    elif set_name not in hits:
        raise PurgeError(f"client {client_id} is not in inbox {set_name}")

    delete, rewrite, kept, review = [], [], [], []
    cdir = data_dir / set_name / "clients" / client_id
    src = sorted(p for p in cdir.rglob("*") if p.is_file()) if cdir.is_dir() else []
    shas, doc_ids = set(), set()
    for p in src:
        h = _sha(p)
        shas.add(h)
        doc_ids.add(f"{client_id}/{p.relative_to(cdir).as_posix()}")
        delete.append({"path": _rel(p, data_dir), "kind": "source image (working copy)", "sha256": h,
                       "bytes": p.stat().st_size})
    sha24 = {h[:24] for h in shas}

    client_entry = None
    cj = data_dir / set_name / "clients.json"
    new, client_entry = _rewrite_clients(cj, client_id)
    if new is not None:
        rewrite.append({"path": _rel(cj, data_dir), "kind": "client list entry", "sha256_before": _sha(cj),
                        "removed": {"client": client_id, "ubn": client_entry.get("ubn"),
                                    "entry_sha256": _sha_bytes(json.dumps(client_entry, ensure_ascii=False,
                                                                          sort_keys=True).encode("utf-8"))}})
    ubn = (client_entry or {}).get("ubn")

    planned = {d["path"] for d in delete}
    if out.is_dir():
        files = sorted(p for p in out.rglob("*") if p.is_file())
        for p in files:
            rel = _rel(p, data_dir)
            n = p.name
            if len(n) > 25 and n[24] == "_" and n[:24] in sha24:
                delete.append({"path": rel, "kind": "cached read (OCR lines, model reply, fields)", "sha256": _sha(p),
                               "bytes": p.stat().st_size})
                planned.add(rel)
            elif p.suffix.lower() in IMG_EXT and p.parent.name != "crops" and _sha(p) in shas:
                delete.append({"path": rel, "kind": "copy of a source image", "sha256": _sha(p),
                               "bytes": p.stat().st_size})
                planned.add(rel)
        for rj in [p for p in files if p.name == "result.json"]:
            new, removed, crops = _rewrite_result(rj, set_name, client_id)
            if new is None:
                continue
            rset = (json.loads(rj.read_text(encoding="utf-8")).get("summary") or {}).get("set")
            rewrite.append({"path": _rel(rj, data_dir), "kind": "run result", "sha256_before": _sha(rj),
                            "removed": removed})
            planned.add(_rel(rj, data_dir))
            for c in crops:
                cp = out / c
                if not cp.is_file():
                    cp = rj.parent / "crops" / Path(c).name
                if cp.is_file() and _rel(cp, data_dir) not in planned:
                    delete.append({"path": _rel(cp, data_dir), "kind": "evidence crop", "sha256": _sha(cp),
                                   "bytes": cp.stat().st_size})
                    planned.add(_rel(cp, data_dir))
            for csvp in sorted(rj.parent.glob("*.csv")):
                newc, nrows = _rewrite_csv(csvp, client_id, doc_ids, rset == set_name)
                if newc is not None:
                    rewrite.append({"path": _rel(csvp, data_dir), "kind": "export", "sha256_before": _sha(csvp),
                                    "removed": {"rows": nrows}, "same_set": rset == set_name})
                    planned.add(_rel(csvp, data_dir))
            if ubn and rset == set_name:
                for txt in rj.parent.glob(f"{ubn}.*"):
                    if txt.is_file() and _rel(txt, data_dir) not in planned:
                        delete.append({"path": _rel(txt, data_dir), "kind": "media-file export (per client)",
                                       "sha256": _sha(txt), "bytes": txt.stat().st_size})
                        planned.add(_rel(txt, data_dir))
        # what is left and still names one of the client's documents
        for p in files:
            rel = _rel(p, data_dir)
            if rel in planned or p.suffix.lower() in IMG_EXT or p.stat().st_size > 50_000_000:
                continue
            try:
                t = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            hit = any(f"{set_name}/clients/{d}" in t for d in doc_ids) or any(h in t for h in shas)
            if not hit:
                continue
            if p.name in ("audit_log.jsonl", "oplog.jsonl"):
                kept.append({"path": rel, "why": KEPT_RULE})
            else:
                review.append({"path": rel, "why": "names this client's documents; not a file Ledger Room writes in "
                                                   "normal use (evaluation output?). Check and delete by hand."})
    return {"client_id": client_id, "set": set_name, "client_ubn": ubn, "data_dir": str(data_dir),
            "doc_ids": sorted(doc_ids),
            "delete": delete, "rewrite": rewrite, "kept": kept, "review": review,
            "totals": {"files_deleted": len(delete), "files_rewritten": len(rewrite),
                       "bytes_deleted": sum(d["bytes"] for d in delete)}}


def _atomic_write(p: Path, b: bytes) -> None:
    tmp = p.with_name(p.name + ".purge-tmp")
    tmp.write_bytes(b)
    os.replace(tmp, p)


def purge(client_id: str, *, reason: str, set_name: str | None = None, operator: str | None = None,
          dry_run: bool = False, data_dir: Path | str = DATA, log_path: Path | None = None,
          key: bytes | None = None) -> dict:
    """Delete one client's images, cached reads, crops and export rows; keep the audit logs; write the deletion
    record (what, when, who, method, SHA-256 of every deleted file) to the operation log. dry_run: plan only."""
    if not (reason or "").strip():
        raise PurgeError("give a --reason (e.g. 'contract ended 2026-10-31')")
    data_dir = Path(data_dir)
    plan = plan_purge(client_id, set_name, data_dir)
    plan["dry_run"] = dry_run
    if dry_run:
        return plan
    from .pipeline import AuditLog   # lazy: the pipeline imports the reader
    log_path = Path(log_path) if log_path else data_dir / "out" / "oplog.jsonl"
    log = AuditLog(log_path, key=key, append=True)
    who = operator or getpass.getuser()
    log.add("purge_start", user=who, client_id=client_id, inbox=plan["set"], client_ubn=plan["client_ubn"],
            reason=reason.strip(), method=METHOD, host=platform.node(), totals=plan["totals"],
            basis={"label": "Bookkeepers' personal-data safeguard regulation Art. 19: after the business ends, record "
                            "how personal data was destroyed", "url": ART19_URL},
            paper_originals="the client keeps the paper originals for five years (帳簿憑證辦法 Art. 27); Ledger "
                            "Room's photos are working copies")
    done_del, done_rw, errors = 0, 0, []
    for it in plan["rewrite"]:
        p = data_dir / it["path"]
        try:
            if it["kind"] == "client list entry":
                new, _ = _rewrite_clients(p, client_id)
            elif it["kind"] == "run result":
                new, _, _ = _rewrite_result(p, plan["set"], client_id)
            else:
                new, _ = _rewrite_csv(p, client_id, set(plan["doc_ids"]), it.get("same_set", False))
            if new is None:
                continue
            _atomic_write(p, new)
            done_rw += 1
            log.add("purge_file", user=who, client_id=client_id, path=it["path"], kind=it["kind"], action="rewritten",
                    sha256_before=it["sha256_before"], sha256_after=_sha_bytes(new), removed=it["removed"])
        except Exception as e:  # noqa: BLE001 - record and continue; the end entry lists failures
            errors.append({"path": it["path"], "error": f"{type(e).__name__}: {e}"})
    for it in plan["delete"]:
        p = data_dir / it["path"]
        try:
            os.remove(p)
            done_del += 1
            log.add("purge_file", user=who, client_id=client_id, path=it["path"], kind=it["kind"], action="deleted",
                    sha256=it["sha256"], bytes=it["bytes"])
        except FileNotFoundError:
            pass
        except OSError as e:
            errors.append({"path": it["path"], "error": f"{type(e).__name__}: {e}"})
    cdir = data_dir / plan["set"] / "clients" / client_id
    if cdir.is_dir():
        for d in sorted((x for x in cdir.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
        try:
            cdir.rmdir()
        except OSError:
            pass
    log.add("purge_end", user=who, client_id=client_id, inbox=plan["set"], files_deleted=done_del,
            files_rewritten=done_rw, errors=errors, kept=[k["path"] for k in plan["kept"]],
            kept_why=KEPT_RULE, left_for_review=[r["path"] for r in plan["review"]])
    plan.update(done={"files_deleted": done_del, "files_rewritten": done_rw}, errors=errors,
                log=str(log_path))
    return plan


# ----------------------------------------------------------------------------------------------------------------
# 3. Encryption at rest: start-up self-check (warn only)
# ----------------------------------------------------------------------------------------------------------------

def _run(cmd: list[str]) -> tuple[int | None, str]:
    """(return code, decoded output); (None, '') when the command is missing or hangs."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    b = (r.stdout or b"") + (r.stderr or b"")
    for enc in ("utf-8", "oem", "mbcs", "cp950"):
        try:
            return r.returncode, b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return r.returncode, b.decode("latin-1")


_ON = re.compile(r"Protection On|保護開啟|保護已開啟|保護為開啟")
_OFF = re.compile(r"Protection Off|保護關閉|保護已關閉|保護為關閉")
_FULL_ENC = re.compile(r"Fully Encrypted|完全加密|已完全加密")
_FULL_DEC = re.compile(r"Fully Decrypted|完全解密|已完全解密")
_PCT = re.compile(r"(?:Percentage Encrypted|加密百分比)\s*[:：]\s*([\d.,]+)\s*%")


def parse_manage_bde(text: str) -> str | None:
    """'encrypted' | 'suspended' | 'not_encrypted' from `manage-bde -status X:` (English or Traditional Chinese
    Windows); None when the output says neither (e.g. 'access denied' without administrator rights)."""
    if not text:
        return None
    m = _PCT.search(text)
    pct = float(m.group(1).replace(",", ".")) if m else None
    if _ON.search(text):
        return "encrypted"
    if _OFF.search(text):
        return "suspended" if (_FULL_ENC.search(text) or pct == 100.0) else "not_encrypted"
    if _FULL_DEC.search(text) or pct == 0.0:
        return "not_encrypted"
    return None


# Windows Shell property System.Volume.BitLockerProtection (readable without administrator rights)
SHELL_BITLOCKER = {1: "encrypted", 3: "encrypting", 6: "encrypted", 2: "not_encrypted", 4: "not_encrypted",
                   0: "not_encrypted", 5: "suspended", 8: "suspended"}


def volume_encryption(path: Path | str, *, run=_run, system: str | None = None) -> dict:
    """{'status': encrypted | encrypting | suspended | not_encrypted | unknown, 'volume', 'method', 'detail'}"""
    system = system or platform.system()
    p = Path(path)
    try:
        p = p.resolve()
    except OSError:
        pass
    if system == "Windows":
        # ntpath, not os.path: the check is also unit-tested on Linux hosts, where os.path has no drive letters
        drive = ntpath.splitdrive(str(p) if platform.system() == "Windows" else str(path))[0]
        if not re.fullmatch(r"[A-Za-z]:", drive or ""):
            return {"status": "unknown", "volume": drive or str(p), "method": "none",
                    "detail": "network share or unusual path: check encryption on the server that holds it"}
        rc, out = run(["manage-bde", "-status", drive])
        st = parse_manage_bde(out)
        if st:
            return {"status": st, "volume": drive, "method": "manage-bde -status", "detail": ""}
        rc2, out2 = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                         f"(New-Object -ComObject Shell.Application).NameSpace('{drive}').Self."
                         "ExtendedProperty('System.Volume.BitLockerProtection')"])
        m = re.search(r"-?\d+", out2 or "")
        if m and int(m.group(0)) in SHELL_BITLOCKER:
            return {"status": SHELL_BITLOCKER[int(m.group(0))], "volume": drive,
                    "method": "Shell property System.Volume.BitLockerProtection",
                    "detail": "manage-bde needs administrator rights" if rc is not None else ""}
        return {"status": "unknown", "volume": drive, "method": "none",
                "detail": "could not read BitLocker status (run as administrator: manage-bde -status " + drive + ")"}
    if system == "Linux":
        rc, src = run(["findmnt", "-n", "-o", "SOURCE", "--target", str(p)])
        src = (src or "").strip().splitlines()[0].strip() if (src or "").strip() else ""
        src = re.sub(r"\[.*\]$", "", src)    # btrfs subvolume suffix: /dev/mapper/x[/@home]
        if not src.startswith("/dev/"):
            return {"status": "unknown", "volume": src or str(p), "method": "findmnt",
                    "detail": "not a block device (network, tmpfs, overlay?) or findmnt missing"}
        rc, types = run(["lsblk", "-s", "-n", "-o", "TYPE", src])
        if rc is None or not (types or "").strip():
            return {"status": "unknown", "volume": src, "method": "lsblk", "detail": "lsblk gave no answer"}
        kinds = {t.strip() for t in types.splitlines() if t.strip()}
        return {"status": "encrypted" if "crypt" in kinds else "not_encrypted", "volume": src,
                "method": "lsblk -s (dm-crypt / LUKS)", "detail": ",".join(sorted(kinds))}
    if system == "Darwin":
        rc, out = run(["fdesetup", "status"])
        if "FileVault is On" in (out or ""):
            return {"status": "encrypted", "volume": "/", "method": "fdesetup status", "detail": ""}
        if "FileVault is Off" in (out or ""):
            return {"status": "not_encrypted", "volume": "/", "method": "fdesetup status", "detail": ""}
    return {"status": "unknown", "volume": str(p), "method": "none", "detail": f"no check for {system}"}


def encryption_self_check(path: Path | str = DATA, *, run=_run, system: str | None = None, warn=None) -> dict:
    """Start-up check of the data folder's volume. Warns (stderr) unless it is encrypted; never raises, never blocks."""
    try:
        r = volume_encryption(path, run=run, system=system)
    except Exception as e:  # noqa: BLE001 - a self-check must not stop the app
        r = {"status": "unknown", "volume": str(path), "method": "none", "detail": f"{type(e).__name__}: {e}"}
    r["path"] = str(path)
    if r["status"] != "encrypted":
        what = {"not_encrypted": "is NOT encrypted", "suspended": "has BitLocker suspended (the key is stored in clear)",
                "encrypting": "is still being encrypted", "unknown": "could not be checked for encryption"}[r["status"]]
        msg = (f"WARNING: the data folder {path} (volume {r['volume']}) {what}. Ledger Room keeps running, but the "
               "safeguard plan expects full-disk encryption (BitLocker / LUKS) on the machine that holds client "
               f"documents; see docs/data_handling.md. {r.get('detail') or ''}").strip()
        r["warning"] = msg
        try:
            (warn or (lambda m: print(m, file=sys.stderr)))(msg)
        except Exception:  # noqa: BLE001
            pass
    return r


# ----------------------------------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ledger_room.privacy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pg = sub.add_parser("purge", help="delete one client's data when the contract ends")
    pg.add_argument("--client", required=True, help="client id, e.g. C01")
    pg.add_argument("--set", help="inbox (data/<set>); needed when the client id is in more than one inbox")
    pg.add_argument("--reason", required=True)
    pg.add_argument("--operator", help="who runs the purge (default: the logged-in OS user)")
    pg.add_argument("--dry-run", action="store_true", help="list what would be deleted; change nothing")
    pg.add_argument("--data", default=str(DATA), help=argparse.SUPPRESS)
    pg.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    ce = sub.add_parser("check-encryption", help="is the data folder on an encrypted volume?")
    ce.add_argument("--path", default=str(DATA))
    a = ap.parse_args(argv)
    if a.cmd == "check-encryption":
        r = encryption_self_check(a.path)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0
    try:
        if not a.dry_run and not a.yes:
            plan = plan_purge(a.client, a.set, a.data)
            t = plan["totals"]
            print(f"About to delete {t['files_deleted']} files ({t['bytes_deleted']} bytes) and rewrite "
                  f"{t['files_rewritten']} for client {a.client} in inbox {plan['set']}. This cannot be undone.")
            if input("Type the client id to confirm: ").strip() != a.client:
                print("cancelled")
                return 1
        r = purge(a.client, reason=a.reason, set_name=a.set, operator=a.operator, dry_run=a.dry_run, data_dir=a.data)
    except PurgeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    show = dict(r)
    if len(show["delete"]) > 40:
        show["delete"] = show["delete"][:40] + [f"... {len(r['delete']) - 40} more"]
    print(json.dumps(show, ensure_ascii=False, indent=1))
    if r["review"]:
        print(f"{len(r['review'])} file(s) still name this client's documents; check them by hand (see 'review').",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
