"""Document reader: OCR (OpenVINO, CPU) + vision-language model (OpenVINO GenAI, Intel GPU).

The model only transcribes. Code then:
  * snaps the model's digit strings (UBN, invoice number) to the exact strings OCR
    found on the page when they differ by <= 2 digits (OCR digits are more reliable
    than a 2B model's transcription; OCR cannot invent a valid-looking number),
  * prefers an OCR date when exactly one date is printed,
  * records the page box of every field so exceptions can show evidence crops,
  * marks each field's provenance (ocr-confirmed / snapped / model-only / ocr).
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
from PIL import Image

from . import normalize as N
from .ocr import Line, read_lines

DEFAULT_MODEL_DIR = os.environ.get("LEDGER_MODEL_DIR", r"E:\models\Qwen3.5-2B-int4-ov")
VLM_MAX_SIDE = int(os.environ.get("LEDGER_VLM_MAX_SIDE", "896"))
CATEGORIES = ["office", "equipment", "materials", "logistics", "software", "printing", "meal", "taxi",
              "lodging", "entertainment", "employee_benefit", "passenger_car", "non_business", "other"]

EXTRACT_PROMPT = """You are reading ONE photo of a Taiwanese bookkeeping document: a 電子發票證明聯 (e_invoice), a 三聯式統一發票 (triplicate_invoice) or a 收據 (receipt).
OCR text lines of the photo (OCR may show simplified characters; its digits are reliable, but it can miss digits written in boxes):
{ocr}

Transcribe the document. Reply with ONE line of JSON only, no markdown, numbers without commas:
{{"type":"e_invoice|triplicate_invoice|receipt","no":"invoice number: 2 letters + 8 digits, or null for a receipt","date":"the transaction date as printed","period":"e-invoice period header such as 115年09-10月, else null","seller":"seller name","seller_ubn":"seller 統一編號: after 賣方, or inside the red seller stamp","buyer":"buyer name or null","buyer_ubn":"buyer 統一編號: after 買方 or 買受人統一編號, else null","items":[{{"d":"item","q":1,"a":100}}],"net":null,"tax":null,"total":0,"note":"text on a yellow sticky note or a handwritten remark; printed footers and amounts in words are NOT notes; else null","category":"one of: {cats}"}}"""


def _fmt_ocr(lines: list[Line]) -> str:
    return "\n".join(f"{l.id}: {l.text}" for l in lines) or "(no text found)"


_INV = re.compile(r"([A-Za-z]{2})\s*[-－]?\s*(\d{8})(?!\d)")
_OCR_DATES = [
    re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"),
    re.compile(r"(1\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})"),   # 日 may be split off by OCR
    re.compile(r"(?<!\d)(1\d{2})/(\d{1,2})/(\d{1,2})(?!\d)"),
]


def ocr_tokens(lines: list[Line]):
    inv, ubn, dates = [], [], []
    for l in lines:
        t = l.text
        for m in _INV.finditer(t):
            inv.append((m.group(1).upper() + m.group(2), l))
        t2 = _INV.sub(" ", t)
        for m in re.finditer(r"(?<!\d)(\d{8})(?!\d)", t2):
            ubn.append((m.group(1), l))
        for rx in _OCR_DATES:
            for m in rx.finditer(t):
                d = N.parse_date("-".join(m.groups()))
                if d:
                    dates.append((d, l))
    return inv, ubn, dates


def _ham(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b)) if len(a) == len(b) else 99


def snap(value: str | None, cands: list[tuple[str, Line]], max_dist: int = 2):
    """Return (value, provenance, line)."""
    if not cands:
        return value, ("model-only" if value else "missing"), None
    if value:
        for tok, l in cands:
            if tok == value:
                return value, "ocr-confirmed", l
        scored = sorted(((_ham(value, tok), tok, l) for tok, l in cands), key=lambda x: x[0])
        best = scored[0]
        tie = len(scored) > 1 and scored[1][0] == best[0] and scored[1][1] != best[1]
        if best[0] <= max_dist and not tie:
            return best[1], "snapped", best[2]
        return value, "model-only", None
    return None, "missing", None


def locate_text(lines: list[Line], needle: str | None, min_ratio: float = 0.6) -> Line | None:
    if not needle:
        return None
    nd = re.sub(r"\s", "", needle)
    nd_digits = re.sub(r"\D", "", needle)
    best, best_s = None, 0.0
    for l in lines:
        t = re.sub(r"\s", "", l.text)
        if nd and nd in t:
            return l
        if nd_digits and len(nd_digits) >= 3 and nd_digits == re.sub(r"\D", "", l.text):
            return l
        common = sum(1 for ch in set(nd) if ch in t) / max(1, len(set(nd)))
        if common > best_s:
            best, best_s = l, common
    return best if best_s >= min_ratio else None


def locate_amount(lines: list[Line], value: int | None) -> Line | None:
    if value is None:
        return None
    for l in lines:
        nums = [N.to_int(x) for x in re.findall(r"\d[\d,]*", l.text)]
        if value in nums:
            return l
    return None


_UBN_LABEL = re.compile(r"[統统][一]?[編编][號号]")
SELLER_HINTS = ("賣方", "卖方", "專用章", "专用章", "電話", "电话", "電话")
BUYER_HINTS = ("買方", "买方", "買受人", "买受人")
BOILERPLATE = ("退貨", "退货", "專用章", "专用章", "大寫", "大写", "新臺幣", "新台币", "合計", "合计", "總計", "总计",
               "SYNTHETIC", "隨機碼", "随机码")


def doc_type_from_ocr(lines: list[Line]) -> str | None:
    text = "".join(l.text for l in lines)
    if any(k in text for k in ("三聯", "三联", "扣抵", "三式")):
        return "triplicate_invoice"
    if any(k in text for k in ("電子發票", "电子发票", "電子發", "证明联", "證明聯", "證明联")):
        return "e_invoice"
    if any(k in text for k in ("收據", "收据", "免用")) or "RECEIPT" in text.upper():
        return "receipt"
    if any(k in text for k in ("買受人", "买受人")):
        return "triplicate_invoice"
    return None


def _near(lines: list[Line], line: Line, radius: float = 2.4) -> list[tuple[float, Line]]:
    h = max(12, line.box[3] - line.box[1])
    cy = (line.box[1] + line.box[3]) / 2
    out = []
    for l in lines:
        dy = abs((l.box[1] + l.box[3]) / 2 - cy)
        if dy <= radius * h:
            out.append((dy, l))
    return sorted(out, key=lambda x: x[0])


def ubn_roles(lines: list[Line]) -> dict[str, list[tuple[str, Line]]]:
    """Assign every printed 8-digit number to seller or buyer from the words around it."""
    roles: dict[str, list[tuple[str, Line]]] = {"seller": [], "buyer": [], "unknown": []}
    for l in lines:
        t = _INV.sub(" ", l.text)
        toks = [(m.group(1), m.start()) for m in re.finditer(r"(?<!\d)(\d{8})(?!\d)", t)]
        if not toks:
            continue
        if any(k in t for k in ("賣方", "卖方", "買方", "买方")):
            for tok, pos in toks:
                before = t[:pos]
                ib = max(before.rfind("買方"), before.rfind("买方"))
                is_ = max(before.rfind("賣方"), before.rfind("卖方"))
                roles["buyer" if ib > is_ else "seller"].append((tok, l))
            continue
        role = "unknown"
        for dy, nl in _near(lines, l):
            if any(k in nl.text for k in BUYER_HINTS):
                role = "buyer"
                break
            if any(k in nl.text for k in SELLER_HINTS):
                role = "seller"
                break
        for tok, _ in toks:
            roles[role].append((tok, l))
    return roles


def buyer_label_line(lines: list[Line]) -> Line | None:
    """The 統一編號 label that belongs to the buyer (used for the zoom re-read)."""
    for l in lines:
        if _UBN_LABEL.search(l.text) and not re.search(r"\d{4}", l.text):
            for dy, nl in _near(lines, l):
                if any(k in nl.text for k in BUYER_HINTS):
                    return l
                if any(k in nl.text for k in SELLER_HINTS):
                    break
    return None


def ocr_amounts(lines: list[Line]) -> list[int]:
    out = []
    for l in lines:
        if any(k in l.text for k in ("電話", "电话", "電话", "No.", "隨機碼", "随机码", "年", "月", "日", "/", ":", "：")) \
                and not any(k in l.text for k in ("總計", "总计", "合計", "合计")):
            continue
        t = _INV.sub(" ", l.text)
        for m in re.finditer(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d{1,6})(?![\d.])", t):
            v = N.to_int(m.group(1))
            if v and v >= 10:
                out.append(v)
    return out


class Reader:
    def __init__(self, model_dir: str = DEFAULT_MODEL_DIR, device: str | None = None):
        import openvino as ov
        import openvino_genai as g
        self._g, self._ov = g, ov
        core = ov.Core()
        self.device = device or os.environ.get("LEDGER_DEVICE") or ("GPU" if any(d.startswith("GPU") for d in core.available_devices) else "CPU")
        self.model_dir = model_dir
        self.model_id = Path(model_dir).name
        t = time.time()
        self.pipe = g.VLMPipeline(model_dir, self.device)
        self.load_s = round(time.time() - t, 1)
        self.lock = threading.Lock()

    def generate(self, prompt: str, image: Image.Image, max_new_tokens: int = 360) -> tuple[str, dict]:
        im = image.convert("RGB")
        s = VLM_MAX_SIDE / max(im.size)
        if s < 1:
            im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
        cfg = self._g.GenerationConfig()
        cfg.max_new_tokens = max_new_tokens
        cfg.do_sample = False
        t = time.time()
        with self.lock:
            r = self.pipe.generate(prompt, image=self._ov.Tensor(np.array(im)[None]), generation_config=cfg)
        pm = r.perf_metrics
        return r.texts[0], {"vlm_s": round(time.time() - t, 2), "ttft_s": round(pm.get_ttft().mean / 1000, 2),
                            "gen_tokens": int(pm.get_num_generated_tokens())}

    def read(self, path: str | Path) -> dict:
        path = Path(path)
        img = Image.open(path).convert("RGB")
        t = time.time()
        lines = read_lines(path)
        ocr_s = round(time.time() - t, 2)
        prompt = EXTRACT_PROMPT.format(ocr=_fmt_ocr(lines), cats=", ".join(CATEGORIES))
        raw, timing = self.generate(prompt, img)
        timing["ocr_s"] = ocr_s
        obj = N.parse_json_loose(raw) or {}
        fields, prov, boxes = postprocess(obj, lines)
        if prov.get("buyer_ubn") == "needs-zoom":
            v, how, box, zt = self.zoom_read_ubn(img, buyer_label_line(lines))
            timing["zoom_s"] = zt
            if v:
                fields["buyer_ubn"], prov["buyer_ubn"] = v, how
                boxes["buyer_ubn"] = box
            else:
                prov["buyer_ubn"] = "model-only" if fields["buyer_ubn"] else "unreadable"
        return {
            "fields": fields, "provenance": prov, "boxes": boxes,
            "ocr_lines": [asdict(l) for l in lines], "raw": raw, "parsed": bool(obj),
            "timing": timing, "model": self.model_id, "device": self.device,
            "image_size": list(img.size),
        }


    def zoom_read_ubn(self, img: Image.Image, label: Line | None):
        """Crop the area right of a 統一編號 label, upscale, OCR it; if that fails ask the VLM about the crop only."""
        t = time.time()
        if label is None:
            return None, None, None, 0.0
        x0, y0, x1, y1 = label.box
        h = y1 - y0
        box = (max(0, x0), max(0, y0 - h), min(img.width, x1 + 520), min(img.height, y1 + h))
        crop = img.crop(box)
        big = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
        zl = read_lines(np.array(big))
        digs = "".join(re.sub(r"\D", "", l.text) for l in sorted(zl, key=lambda l: l.box[0]))
        if len(digs) == 8:
            return digs, "ocr-zoom", list(box), round(time.time() - t, 2)
        txt, _ = self.generate("This crop shows a Taiwanese Unified Business Number (統一編號): 8 digits, "
                               "possibly one digit per box. Reply with the 8 digits only.", big, max_new_tokens=16)
        d = re.sub(r"\D", "", txt)
        if len(d) == 8:
            return d, "vlm-zoom", list(box), round(time.time() - t, 2)
        return None, None, None, round(time.time() - t, 2)


def postprocess(obj: dict, lines: list[Line]) -> tuple[dict, dict, dict]:
    inv_c, ubn_c, date_c = ocr_tokens(lines)
    prov, boxes = {}, {}

    def put_box(name, line):
        if line is not None:
            boxes[name] = list(line.box)

    dtype_model = N.clean_str(obj.get("type"))
    dtype = doc_type_from_ocr(lines)
    prov["doc_type"] = "ocr-keyword" if dtype else "model-only"
    if dtype is None and dtype_model in ("e_invoice", "triplicate_invoice", "receipt"):
        dtype = dtype_model

    # invoice number
    inv_raw = N.clean_str(obj.get("no"))
    inv = None
    if inv_raw:
        m = re.search(r"([A-Za-z]{2})\s*[-－]?\s*(\d{8})", inv_raw)
        inv = (m.group(1).upper() + m.group(2)) if m else None
    inv, prov["invoice_no"], l = snap(inv, inv_c)
    if inv is None and len({t for t, _ in inv_c}) == 1 and dtype != "receipt":
        inv, prov["invoice_no"], l = inv_c[0][0], "ocr", inv_c[0][1]
    put_box("invoice_no", l)

    # UBNs: page context decides the role; the model fills gaps
    roles = ubn_roles(lines)
    has_buyer_label = any(any(k in l.text for k in BUYER_HINTS) for l in lines)
    out_ubn = {}
    for role in ("seller", "buyer"):
        v = N.digits(obj.get(f"{role}_ubn"))
        v = v if v and len(v) == 8 else (v or None)
        mine = list(dict.fromkeys(tok for tok, _ in roles[role]))
        if len(mine) == 1:
            tok = mine[0]
            prov[f"{role}_ubn"] = "ocr-confirmed" if v == tok else "ocr-role"
            out_ubn[role] = tok
            put_box(f"{role}_ubn", next(l for t, l in roles[role] if t == tok))
            continue
        other = "buyer" if role == "seller" else "seller"
        pool = roles[role] + roles["unknown"]
        if v and len(v) == 8:
            v2, how, l = snap(v, pool) if pool else (v, "model-only", None)
            if v2 in {t for t, _ in roles[other]}:
                v2, how, l = None, "dropped-role-conflict", None
            v, prov[f"{role}_ubn"] = v2, how
        else:
            prov[f"{role}_ubn"] = "model-only" if v else "missing"
            l = None
        if role == "buyer" and not has_buyer_label:
            v, prov["buyer_ubn"], l = None, "no-buyer-label", None
        if role == "buyer" and has_buyer_label and dtype == "triplicate_invoice" \
                and (v is None or prov["buyer_ubn"] == "model-only"):
            prov["buyer_ubn"] = "needs-zoom"   # nothing on the page confirms the model: zoom in and re-read
        out_ubn[role] = v
        put_box(f"{role}_ubn", l)

    # date: prefer a single unambiguous OCR date
    d_model = N.parse_date(obj.get("date"))
    uniq = sorted({d for d, _ in date_c})
    if len(uniq) == 1:
        d = uniq[0]
        prov["date"] = "ocr-confirmed" if d_model == d else "ocr"
        put_box("date", next(l for dd, l in date_c if dd == d))
    else:
        d = d_model
        prov["date"] = "model-only" if d else "missing"
        if d:
            put_box("date", next((l for dd, l in date_c if dd == d), None))

    period = N.parse_period(obj.get("period"))
    if period is None:   # fall back to a printed header in OCR
        for l in lines:
            p = N.parse_period(l.text)
            if p:
                period = p
                put_box("invoice_period", l)
                break
    else:
        for l in lines:
            if N.parse_period(l.text) == period:
                put_box("invoice_period", l)
                break

    items = []
    for it in obj.get("items") or []:
        if isinstance(it, dict):
            desc = N.clean_str(it.get("d") or it.get("desc"))
            items.append({"desc": desc or "", "qty": N.to_int(it.get("q") or it.get("qty")),
                          "amount": N.to_int(it.get("a") or it.get("amount"))})
        elif isinstance(it, (list, tuple)) and it:
            items.append({"desc": str(it[0]), "qty": N.to_int(it[1]) if len(it) > 1 else None,
                          "amount": N.to_int(it[-1]) if len(it) > 2 else None})
    item_lines = [locate_text(lines, i["desc"]) for i in items]
    if item_lines and item_lines[0] is not None:
        put_box("items", item_lines[0])

    total, net, tax = N.to_int(obj.get("total")), N.to_int(obj.get("net")), N.to_int(obj.get("tax"))
    amts = ocr_amounts(lines)
    if total is not None and total in amts:
        prov["total"] = "ocr-confirmed"
    elif amts:
        total, prov["total"] = max(amts), "ocr-max"
    else:
        prov["total"] = "model-only" if total is not None else "missing"
    put_box("total", locate_amount(lines, total))
    note = N.clean_str(obj.get("note"))
    words = re.sub(r"[\s零壹貳贰參叁肆伍陸陆柒捌玖拾佰仟萬万元整]", "", note or "")
    if note and (any(k in note for k in BOILERPLATE) or note.startswith("text on") or note.startswith("handwritten")
                 or not words):   # an amount written in words is not a note
        note, prov["note"] = None, "dropped-boilerplate"
    note_line = locate_text(lines, note, 0.75) if note else None
    if note and note_line is None:
        note, prov["note"] = None, "dropped-not-on-page"
    put_box("note", note_line)
    cat = N.clean_str(obj.get("category"))
    fields = {
        "doc_type": dtype, "invoice_no": inv, "date": d.isoformat() if d else None,
        "invoice_period": period if dtype == "e_invoice" or (dtype is None and period) else None,
        "seller_name": N.clean_str(obj.get("seller")), "seller_ubn": out_ubn["seller"],
        "buyer_name": N.clean_str(obj.get("buyer")), "buyer_ubn": out_ubn["buyer"],
        "items": items, "net": net, "tax": tax, "total": total, "note": note,
        "category": cat if cat in CATEGORIES else None,
    }
    put_box("seller_name", locate_text(lines, fields["seller_name"]))
    return fields, prov, boxes
