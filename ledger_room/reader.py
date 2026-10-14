"""Document reader: OCR (OpenVINO, CPU) + vision-language model (OpenVINO GenAI, Intel GPU).

The model only transcribes. Code then reconciles what it said with what is printed:
  * document type from printed keywords (三聯式 / 電子發票證明聯 / 收據),
  * invoice number and UBN digits snapped to the exact OCR strings (<= 2 digits apart),
  * UBN roles (seller / buyer) from the words around each number, so a model that copies
    the seller's number into the buyer field is overruled,
  * boxed digits that OCR misses: crop next to the label, keep only dark ink (colored form
    lines and the red stamp disappear), enlarge, read again with OCR and with the model,
    and accept a value only when two of three independent reads agree,
  * a single printed date wins over the model's date,
  * model items that are not on the page are dropped; item rows OCR finds in the item
    table are added,
  * every field keeps its provenance and its page box (for evidence crops).
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import Counter
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
CONFIRMED = {"ocr-confirmed", "ocr-role", "snapped", "ocr", "ocr-keyword", "ocr-total-line", "zoom-vote 2/3", "zoom-vote 3/3"}

EXTRACT_PROMPT = """You are reading ONE photo of a Taiwanese bookkeeping document: a 電子發票證明聯 (e_invoice), a 三聯式統一發票 (triplicate_invoice) or a 收據 (receipt).
OCR text lines of the photo (OCR may show simplified characters; its digits are reliable, but it can miss digits written in boxes):
{ocr}

Transcribe the document. Reply with ONE line of JSON only, no markdown, numbers without commas:
{{"type":"e_invoice|triplicate_invoice|receipt","no":"invoice number: 2 letters + 8 digits, or null for a receipt","date":"the transaction date as printed","period":"e-invoice period header such as 115年09-10月, else null","seller":"seller name","seller_ubn":"seller 統一編號: after 賣方, or inside the red seller stamp","buyer":"buyer name or null","buyer_ubn":"buyer 統一編號: after 買方 or 買受人統一編號, else null","items":[{{"d":"item","q":1,"a":100}}],"net":null,"tax":null,"total":0,"note":"text on a yellow sticky note or a handwritten remark; printed footers and amounts in words are NOT notes; else null","category":"one of: {cats}"}}"""

# Same turn format the Qwen3.5-2B chat template produces (user turn with the image first, empty think block).
WRAP = "<|im_start|>user" + chr(10) + "<ov_genai_image_0>{prompt}<|im_end|>" + chr(10) + "<|im_start|>assistant" + chr(10) \
    + "<think>" + chr(10) * 2 + "</think>" + chr(10) * 2

ZOOM_PROMPT = ("This crop shows a Taiwanese Unified Business Number (統一編號): 8 digits, possibly one digit per box. "
               "Reply with the 8 digits only.")

_INV = re.compile(r"([A-Za-z]{2})\s*[-－]?\s*(\d{8})(?!\d)")
_OCR_DATES = [
    re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"),
    re.compile(r"(1\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})"),   # 日 may be split off by OCR
    re.compile(r"(?<!\d)(1\d{2})/(\d{1,2})/(\d{1,2})(?!\d)"),
]
_UBN_LABEL = re.compile(r"[統统]一?[編编]?[號号]|[編编][號号]")   # OCR sometimes drops a character
SELLER_HINTS = ("賣方", "卖方", "專用章", "专用章", "電話", "电话", "電话")
BUYER_HINTS = ("買方", "买方", "買受人", "买受人")
BOILERPLATE = ("退貨", "退货", "專用章", "专用章", "大寫", "大写", "新臺幣", "新台币", "合計", "合计", "總計", "总计",
               "SYNTHETIC", "隨機碼", "随机码")
ITEM_HEAD = ("品名", "品 名", "交易明細", "交易明细", "Item")
ITEM_END = ("合計", "合计", "銷售額", "销售额", "總計", "总计")
TABLE_LABELS = ("數量", "数量", "單價", "单价", "金額", "金额", "備註", "备注", "備", "品名", "Qty", "Amount")
PLACEHOLDERS = {"", "item", "d", "desc", "品名", "null"}


def _fmt_ocr(lines: list[Line]) -> str:
    return "\n".join(f"{l.id}: {l.text}" for l in lines) or "(no text found)"


def _cjk(s: str) -> int:
    return sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")


name_sim = N.name_sim


def ocr_tokens(lines: list[Line]):
    inv, dates = [], []
    for l in lines:
        for m in _INV.finditer(l.text):
            inv.append((m.group(1).upper() + m.group(2), l))
        for rx in _OCR_DATES:
            for m in rx.finditer(l.text):
                d = N.parse_date("-".join(m.groups()))
                if d:
                    dates.append((d, l))
    return inv, dates


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


def doc_type_from_ocr(lines: list[Line]) -> str | None:
    text = "".join(l.text for l in lines)
    if any(k in text for k in ("三聯", "三联", "扣抵", "三式")):
        return "triplicate_invoice"
    if any(k in text for k in ("二聯式", "二联式", "收銀機統一發票", "收银机统一发票", "收銀機統一发票")):
        return "two_part_register"
    if any(k in text for k in ("電子發票", "电子发票", "電子發", "证明联", "證明聯", "證明联")):
        return "e_invoice"
    if any(k in text for k in ("收據", "收据", "免用")) or "RECEIPT" in text.upper():
        return "receipt"
    if any(k in text for k in ("買受人", "买受人")):
        return "triplicate_invoice"
    return None


def _near(lines: list[Line], line: Line, radius: float = 3.2) -> list[tuple[float, Line]]:
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
        if re.search(r"(?<![買买])方\s*[:：]?\s*\d{8}", t) and not any(k in t for k in ("買方", "买方")):
            for tok, _ in toks:          # OCR dropped the 賣 of 賣方
                roles["seller"].append((tok, l))
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
    # a buyer label owns one number: if one buyer number carries its own 買方 label, the others are not the buyer's
    labelled = [(t, l) for t, l in roles["buyer"] if any(k in l.text for k in BUYER_HINTS)]
    if labelled and len({t for t, _ in roles["buyer"]}) > 1:
        roles["unknown"] += [(t, l) for t, l in roles["buyer"] if (t, l) not in labelled]
        roles["buyer"] = labelled
    unknown = list(dict.fromkeys(t for t, _ in roles["unknown"]))
    if not roles["seller"] and len(unknown) == 1:   # a lone unlabelled UBN on a voucher is the issuer's
        roles["seller"] = [x for x in roles["unknown"]]
        roles["unknown"] = []
    return roles


def buyer_label_line(lines: list[Line]) -> Line | None:
    """The 統一編號 label that belongs to the buyer (used for the zoom re-read)."""
    for l in lines:
        if _UBN_LABEL.search(l.text) and not re.search(r"\d{4}", l.text):
            for dy, nl in _near(lines, l, 2.4):
                if any(k in nl.text for k in BUYER_HINTS):
                    return l
                if any(k in nl.text for k in SELLER_HINTS):
                    break
    return None


def ocr_amounts(lines: list[Line]) -> list[int]:
    out = []
    for l in lines:
        if re.search(r"[隨随][機机][碼码]|[電电][話话]", l.text):
            continue
        if any(k in l.text for k in ("No.", "年", "月", "日", "/", ":", "：")) \
                and not any(k in l.text for k in ("總計", "总计", "合計", "合计")):
            continue
        t = _INV.sub(" ", l.text)
        for m in re.finditer(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d{1,6})(?![\d.])", t):
            v = N.to_int(m.group(1))
            if v and v >= 10:
                out.append(v)
    return out


def ocr_total_line_amounts(lines: list[Line]) -> list[int]:
    """Numbers printed on (or on the same row as) a 總計 / 合計 label."""
    out = []
    labels = [l for l in lines if re.search(r"[總总]計?|[總总]计|[合][計计]", l.text) and "新" not in l.text]
    for lab in labels:
        cands = [lab] + [l for l in lines if l is not lab and abs((l.box[1] + l.box[3]) / 2 - (lab.box[1] + lab.box[3]) / 2)
                         < 0.8 * (lab.box[3] - lab.box[1]) and l.box[0] > lab.box[0]]
        for l in cands:
            for m in re.finditer(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d{1,6})(?![\d.])", _INV.sub(" ", l.text)):
                v = N.to_int(m.group(1))
                if v and v >= 10:
                    out.append(v)
    return out


def ocr_item_lines(lines: list[Line]) -> list[Line]:
    """Text rows inside the item table: below the 品名/交易明細 header, above the first total row."""
    head = next((l for l in sorted(lines, key=lambda l: l.box[1]) if any(k in l.text for k in ITEM_HEAD)), None)
    if head is None:
        return []
    ends = [l for l in lines if l.box[1] > head.box[3] and any(k in l.text for k in ITEM_END)]
    end_y = min((l.box[1] for l in ends), default=head.box[3] + 400)
    out = []
    for l in lines:
        cy = (l.box[1] + l.box[3]) / 2
        if head.box[3] - 4 <= cy <= end_y and _cjk(l.text) >= 2 and not any(k == l.text.strip() for k in TABLE_LABELS) \
                and not any(k in l.text for k in ITEM_HEAD):
            out.append(l)
    return out


def ink_only(img: Image.Image) -> Image.Image:
    """Keep dark, low-saturation pixels (printed or written digits); drop colored form lines and the red stamp."""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    mx, mn = a.max(axis=2), a.min(axis=2)
    ink = (mx < 125) & ((mx - mn) < 60)
    out = np.full(a.shape[:2], 255, np.uint8)
    out[ink] = 0
    return Image.fromarray(out).convert("RGB")


def _ink(q: np.ndarray) -> np.ndarray:
    mx, mn = q.max(axis=2), q.min(axis=2)
    return (mx < 120) & ((mx - mn) < 40)


def box_cells(crop: Image.Image):
    """Find the printed digit boxes (colored vertical form lines) and return the cells between them."""
    q = np.asarray(crop.convert("RGB")).astype(np.int16)
    r, g, b = q[..., 0], q[..., 1], q[..., 2]
    mx, mn = q.max(axis=2), q.min(axis=2)
    colored = ((mx - mn) > 35) & ~((r > g + 40) & (r > b + 40))   # form-line ink, not the red stamp
    line_cols = np.where(colored.mean(axis=0) > 0.25)[0]
    if line_cols.size == 0:
        return None
    groups = [[line_cols[0], line_cols[0]]]
    for x in line_cols[1:]:
        if x - groups[-1][1] <= 2:
            groups[-1][1] = x
        else:
            groups.append([x, x])
    cells = [(a1 + 1, b0) for (a0, a1), (b0, b1) in zip(groups, groups[1:]) if b0 - a1 > 12]
    ys = np.where(colored.mean(axis=1) > 0.25)[0]
    span = (int(ys.min()), int(ys.max())) if ys.size else (0, q.shape[0])
    return cells, span


def read_digit_cell(cell: Image.Image) -> str:
    """Recognise one boxed digit: keep its ink, centre it on a white square, run the OCR recogniser only."""
    from .ocr import recognise_only
    m = _ink(np.asarray(cell.convert("RGB")).astype(np.int16))
    if not m.any():
        return ""
    yy, xx = np.where(m)
    m = m[yy.min(): yy.max() + 1, xx.min(): xx.max() + 1]
    h, w = m.shape
    side = max(h, w) + 16
    canvas = np.full((side, side), 255, np.uint8)
    canvas[(side - h) // 2:(side - h) // 2 + h, (side - w) // 2:(side - w) // 2 + w] = np.where(m, 0, 255)
    im = Image.fromarray(canvas).convert("RGB").resize((48, 48), Image.LANCZOS)
    return re.sub(r"\D", "", recognise_only(np.asarray(im)))[:1]


def read_boxed_digits(crop: Image.Image) -> str | None:
    r = box_cells(crop)
    if not r:
        return None
    cells, (ya, yb) = r
    if len(cells) != 8:
        return None
    digs = "".join(read_digit_cell(crop.crop((a + 2, ya + 3, b - 2, yb - 3))) for a, b in cells)
    return digs if len(digs) == 8 else None


def seller_name_from_ocr(lines: list[Line], seller_line: Line | None, dtype: str | None) -> tuple[str | None, Line | None]:
    if seller_line is not None:   # the line just above the seller UBN inside the (possibly rotated) stamp
        sh = seller_line.box[3] - seller_line.box[1]
        scy = (seller_line.box[1] + seller_line.box[3]) / 2
        above = []
        for l in lines:
            cy = (l.box[1] + l.box[3]) / 2
            x_overlap = min(l.box[2], seller_line.box[2]) - max(l.box[0], seller_line.box[0])
            if 0.3 * sh < scy - cy < 3 * sh and x_overlap > 0 and _cjk(l.text) >= 3 \
                    and not re.search(r"\d{3}", l.text) and not _UBN_LABEL.search(l.text):
                above.append(l)
        if above:
            l = max(above, key=lambda l: l.box[3])
            return l.text, l
    if dtype == "e_invoice":
        top = sorted(lines, key=lambda l: l.box[1])[:2]
        for l in top:
            if _cjk(l.text) >= 3 and "證明" not in l.text and "证明" not in l.text:
                return l.text, l
    return None, None


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
        # Qwen3.5-2B answers directly by default; the 9B chat template opens a <think> block unless told not to.
        # Ledger Room wants a transcription, not reasoning, so every reader runs with thinking off (the 2B default).
        # This pipeline cannot swap its chat template, so for such models the prompt is wrapped here in the same
        # format the 2B template produces, with an empty think block.
        self.thinking = "off"
        self.wrap = False
        tp = Path(model_dir) / "chat_template.jinja"
        if tp.exists() and "enable_thinking is defined and enable_thinking is false" in tp.read_text(encoding="utf-8"):
            self.wrap = True
            self.thinking = "off (template default was on; prompt wrapped by Ledger Room)"

    def generate(self, prompt: str, image: Image.Image, max_new_tokens: int = 360) -> tuple[str, dict]:
        im = image.convert("RGB")
        s = VLM_MAX_SIDE / max(im.size)
        if s < 1:
            im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
        cfg = self._g.GenerationConfig()
        cfg.max_new_tokens = max_new_tokens
        cfg.do_sample = False
        if self.wrap:
            cfg.apply_chat_template = False
            prompt = WRAP.format(prompt=prompt)
        t = time.time()
        with self.lock:
            r = self.pipe.generate(prompt, image=self._ov.Tensor(np.array(im)[None]), generation_config=cfg)
        pm = r.perf_metrics
        return r.texts[0], {"vlm_s": round(time.time() - t, 2), "ttft_s": round(pm.get_ttft().mean / 1000, 2),
                            "gen_tokens": int(pm.get_num_generated_tokens())}

    def read_raw(self, path: str | Path) -> dict:
        """OCR + one model call. Cached by the pipeline separately from post-processing."""
        path = Path(path)
        img = Image.open(path).convert("RGB")
        t = time.time()
        lines = read_lines(path)
        ocr_s = round(time.time() - t, 2)
        prompt = EXTRACT_PROMPT.format(ocr=_fmt_ocr(lines), cats=", ".join(CATEGORIES))
        raw, timing = self.generate(prompt, img)
        timing["ocr_s"] = ocr_s
        return {"ocr_lines": [asdict(l) for l in lines], "raw": raw, "timing": timing, "image_size": list(img.size),
                "model": self.model_id, "device": self.device}

    def read(self, path: str | Path, raw: dict | None = None) -> dict:
        path = Path(path)
        raw = raw or self.read_raw(path)
        lines = [Line(l["id"], l["text"], tuple(l["box"]), l["conf"]) for l in raw["ocr_lines"]]
        timing = dict(raw["timing"])
        obj = N.parse_json_loose(raw["raw"]) or {}
        fields, prov, boxes, extra = postprocess(obj, lines)
        if prov.get("buyer_ubn") == "needs-zoom":
            img = Image.open(path).convert("RGB")
            v, how, box, cands, zt = self.zoom_vote(img, buyer_label_line(lines), extra.get("buyer_ubn_model"))
            timing["zoom_s"] = zt
            fields["buyer_ubn"], prov["buyer_ubn"] = v, how
            extra["buyer_ubn_reads"] = cands
            if box:
                boxes["buyer_ubn"] = box
        timing["total_s"] = round(sum(timing.get(k, 0) for k in ("ocr_s", "vlm_s", "zoom_s")), 2)
        return {
            "fields": fields, "provenance": prov, "boxes": boxes, "extra": extra,
            "ocr_lines": raw["ocr_lines"], "raw": raw["raw"], "parsed": bool(obj),
            "timing": timing, "model": raw.get("model", self.model_id), "device": raw.get("device", self.device),
            "image_size": raw["image_size"],
        }

    def zoom_vote(self, img: Image.Image, label: Line | None, model_value: str | None):
        """Three independent reads of a boxed UBN; accept only a value two of them agree on."""
        t = time.time()
        reads = {"model_page": model_value if model_value and len(model_value) == 8 else None,
                 "ocr_zoom": None, "vlm_zoom": None}
        box = None
        if label is not None:
            x0, y0, x1, y1 = label.box
            h = y1 - y0
            box = [max(0, x1 - 4), max(0, int(y0 - 0.8 * h)), min(img.width, x1 + 520), min(img.height, int(y1 + 0.8 * h))]
            crop = img.crop(box)
            reads["ocr_zoom"] = read_boxed_digits(crop)       # one digit per printed box
            clean = ink_only(crop)
            big = clean.resize((clean.width * 2, clean.height * 2), Image.LANCZOS)
            txt, _ = self.generate(ZOOM_PROMPT, big, max_new_tokens=16)
            d = re.sub(r"\D", "", txt)
            reads["vlm_zoom"] = d if len(d) == 8 else None
        votes = Counter(v for v in reads.values() if v)
        zt = round(time.time() - t, 2)
        if votes:
            val, n = votes.most_common(1)[0]
            if n >= 2:
                return val, f"zoom-vote {n}/3", box, reads, zt
        return None, "unconfirmed", box, reads, zt


def postprocess(obj: dict, lines: list[Line]) -> tuple[dict, dict, dict, dict]:
    inv_c, date_c = ocr_tokens(lines)
    prov, boxes, extra = {}, {}, {}

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
    out_ubn, ubn_line = {}, {}
    for role in ("seller", "buyer"):
        v = N.digits(obj.get(f"{role}_ubn"))
        v = v if v and len(v) == 8 else (v or None)
        if role == "buyer":
            extra["buyer_ubn_model"] = v
        mine = list(dict.fromkeys(tok for tok, _ in roles[role]))
        if len(mine) == 1:
            tok = mine[0]
            prov[f"{role}_ubn"] = "ocr-confirmed" if v == tok else "ocr-role"
            out_ubn[role] = tok
            ubn_line[role] = next(l for t, l in roles[role] if t == tok)
            put_box(f"{role}_ubn", ubn_line[role])
            continue
        other = "buyer" if role == "seller" else "seller"
        pool = roles[role] + roles["unknown"]
        l = None
        if v and len(v) == 8:
            v2, how, l = snap(v, pool) if pool else (v, "model-only", None)
            if v2 in {t for t, _ in roles[other]}:
                v2, how, l = None, "dropped-role-conflict", None
            v, prov[f"{role}_ubn"] = v2, how
        else:
            prov[f"{role}_ubn"] = "model-only" if v else "missing"
        if role == "buyer" and not has_buyer_label:
            v, prov["buyer_ubn"], l = None, "no-buyer-label", None
        if role == "buyer" and has_buyer_label and dtype == "triplicate_invoice" \
                and prov["buyer_ubn"] not in ("ocr-confirmed", "snapped"):
            prov["buyer_ubn"] = "needs-zoom"   # nothing on the page confirms it: zoom in and vote
        out_ubn[role] = v
        ubn_line[role] = l
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

    # items: keep model items that are on the page, add OCR rows from the item table
    items = []
    for it in obj.get("items") or []:
        if isinstance(it, dict):
            desc = N.clean_str(it.get("d") or it.get("desc")) or ""
            q, a = it.get("q") or it.get("qty"), it.get("a") or it.get("amount")
        elif isinstance(it, (list, tuple)) and it:
            desc, q, a = str(it[0]), (it[1] if len(it) > 1 else None), (it[-1] if len(it) > 2 else None)
        else:
            continue
        if desc.strip().lower() in PLACEHOLDERS or locate_text(lines, desc, 0.6) is None:
            continue
        items.append({"desc": desc, "qty": N.to_int(q), "amount": N.to_int(a), "source": "model"})
    item_rows = ocr_item_lines(lines)
    for l in item_rows:
        if not any(name_sim(l.text, i["desc"]) >= 0.6 for i in items):
            items.append({"desc": l.text, "qty": None, "amount": None, "source": "ocr"})
    prov["items"] = "model+ocr" if any(i["source"] == "ocr" for i in items) else ("model" if items else "missing")
    first = next((locate_text(lines, i["desc"]) for i in items), None)
    put_box("items", first)

    total, net, tax = N.to_int(obj.get("total")), N.to_int(obj.get("net")), N.to_int(obj.get("tax"))
    amts = ocr_amounts(lines)
    tl = ocr_total_line_amounts(lines)
    if total is not None and total in amts:
        prov["total"] = "ocr-confirmed"
    elif tl:
        total, prov["total"] = max(tl), "ocr-total-line"
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

    seller_name = N.clean_str(obj.get("seller"))
    buyer_name = N.clean_str(obj.get("buyer"))
    prov["seller_name"] = "model"
    if not seller_name or (buyer_name and name_sim(seller_name, buyer_name) >= 0.8):
        nm, nl = seller_name_from_ocr(lines, ubn_line.get("seller"), dtype)
        if nm:
            seller_name, prov["seller_name"] = nm, "ocr-stamp"
            put_box("seller_name", nl)
    if "seller_name" not in boxes:
        put_box("seller_name", locate_text(lines, seller_name))

    cat = N.clean_str(obj.get("category"))
    fields = {
        "doc_type": dtype, "invoice_no": inv, "date": d.isoformat() if d else None,
        "invoice_period": period if dtype == "e_invoice" or (dtype is None and period) else None,
        "seller_name": seller_name, "seller_ubn": out_ubn["seller"],
        "buyer_name": buyer_name, "buyer_ubn": out_ubn["buyer"],
        "items": items, "net": net, "tax": tax, "total": total, "note": note,
        "category": cat if cat in CATEGORIES else None,
    }
    return fields, prov, boxes, extra
