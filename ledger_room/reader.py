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

import hashlib
import math
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


# ---------------- amounts: the model reads, code decides ----------------
# The total is the number most often misread on real receipts: a reader takes cash paid (現金 / 實收), change (找零),
# a card balance or a long serial number instead. Code collects what is printed (labelled rows, the e-invoice header
# slot, net + tax = total triples, the amount in words), drops numbers on payment rows, and accepts a total only when
# two independent readings agree. Otherwise the total is marked unconfirmed and a person checks it (READ_REVIEW).

TOTAL_ASK_PROMPT = """This photo is ONE Taiwanese receipt or invoice. OCR text lines of the photo (digits are reliable, but OCR can drop characters):
{ocr}

What is the TOTAL AMOUNT of this purchase (總計 / 合計 / 應付金額 / 總金額 / 銷售總額)? Do NOT answer cash handed over (現金 / 實收), change (找零), a card or stored-value balance (餘額), points, a deposit (訂金), the net amount or tax alone, or any serial, order, card or phone number.
Reply with the number only, digits without commas. Reply none if no total is printed."""
TOTAL_ASK_KEY = hashlib.sha256(TOTAL_ASK_PROMPT.encode("utf-8")).hexdigest()[:12]   # cache key for answers

_FOLD = str.maketrans({"总": "總", "计": "計", "额": "額", "税": "稅", "应": "應", "实": "實", "现": "現", "余": "餘",
                       "点": "點", "号": "號", "码": "碼", "随": "隨", "机": "機", "编": "編", "订": "訂", "单": "單",
                       "销": "銷", "营": "營", "业": "業", "载": "載", "会": "會", "员": "員", "帐": "帳", "账": "帳",
                       "权": "權", "数": "數", "统": "統", "电": "電", "话": "話", "买": "買", "卖": "賣", "费": "費",
                       "扣": "扣", "币": "幣", "台": "臺", "写": "寫", "贰": "貳", "叁": "參", "参": "參", "陆": "陸",
                       "万": "萬", "圆": "元", "选": "選", "储": "儲", "值": "值", "规": "機"})
_SP = re.compile(r"[\s　]")
# order matters: a payment row is never the total; 銷售額合計 is the net, not the total
EXCLUDE_LABEL = re.compile(r"現金|實收|收現|找零|找拿|找回|找|付款|您付|已付|支付|刷卡|信用卡|悠遊卡|悠游卡|一卡通|儲值|餘額|"
                           r"點數|卡號|序號|訂金|尾款|授權|帳號|電話|編號|統編|載具|會員|訂單|號碼|碼|機號|店號|端末|代號|"
                           r"人數|數量|件數|折抵|折扣|找續|格式|LINE|Pay|PAY|No|POS|TEL|Tel")
NET_LABEL = re.compile(r"銷售額|未稅|稅前")
TAX_LABEL = re.compile(r"稅額|營業稅|稅金")
WORDS_LABEL = re.compile(r"新臺幣|大寫")
TOTAL_LABEL = re.compile(r"總計|總金額|總額|應付|應收|銷售總|合計|總價|總.?[:：]?$")
WEAK_TOTAL_LABEL = re.compile(r"小計|^計[:：]?$|^[總計][^\d]?$")
PAYMENT_SLIP_PAID = re.compile(r"您付|付款金額|支付金額|交易金額")
_NUM_TOK = re.compile(r"\d{1,3}(?:[,.]\d{3})+(?!\d)|\d+")
_UNIT_AFTER = set("份入人吋埠件項年月日號樓巷弄段之時分秒個盒張瓶包箱支片條天晚克x×*%")
_CN_DIG = {"零": 0, "壹": 1, "貳": 2, "參": 3, "肆": 4, "伍": 5, "陸": 6, "柒": 7, "捌": 8, "玖": 9}
_CN_UNIT = {"拾": 10, "佰": 100, "仟": 1000}
_CN_WORDS = re.compile(r"[零壹貳參肆伍陸柒捌玖拾佰仟萬]{2,}元")


def _fold(s: str) -> str:
    return _SP.sub("", s or "").translate(_FOLD)


def cn_words_to_int(s: str) -> int | None:
    """'參仟壹佰壹拾捌元整' -> 3118. None when the string is not a clean 大寫 amount."""
    m = _CN_WORDS.search(_fold(s))
    if not m:
        return None
    total, section, digit = 0, 0, None
    for ch in m.group(0)[:-1]:
        if ch in _CN_DIG:
            if digit is not None and _CN_DIG[ch] != 0:
                return None   # two digits in a row: not a clean amount in words
            digit = _CN_DIG[ch] or None
        elif ch in _CN_UNIT:
            if digit is None:
                return None
            section += digit * _CN_UNIT[ch]
            digit = None
        elif ch == "萬":
            section += digit or 0
            total += section * 10000
            section, digit = 0, None
    total += section + (digit or 0)
    return total or None


def _line_h(lines: list[Line]) -> float:
    """Typical text height: median box height of short lines (a long line's box grows when the photo is tilted)."""
    hs = sorted(l.box[3] - l.box[1] for l in lines if (l.box[2] - l.box[0]) < 4 * (l.box[3] - l.box[1]))
    return float(hs[len(hs) // 2]) if hs else 24.0


def estimate_skew(lines: list[Line]) -> float:
    """Tilt of the photo (tan of the angle), from how text boxes line up into rows: the slope that puts the most
    pairs of boxes far apart horizontally on the same row. 0 when nothing lines up (a single column of text)."""
    if len(lines) < 4:
        return 0.0
    h = _line_h(lines)
    pts = [((l.box[0] + l.box[2]) / 2, (l.box[1] + l.box[3]) / 2) for l in lines]
    pairs = [(x1 - x2, y1 - y2) for i, (x1, y1) in enumerate(pts) for (x2, y2) in pts[i + 1:] if abs(x1 - x2) > 4 * h]
    if not pairs:
        return 0.0
    best, best_n = 0.0, -1
    for k in sorted(range(-28, 29), key=abs):          # -7 .. +7 degrees in 0.25 degree steps, smallest tilt first
        t = math.tan(math.radians(k * 0.25))
        n = sum(1 for dx, dy in pairs if abs(dy - dx * t) < 0.3 * h)
        if n > best_n:
            best, best_n = t, n
    return best


def _row(lines: list[Line], l: Line, tol: float = 0.6, skew: float = 0.0) -> list[Line]:
    h = _line_h(lines) if skew else max(10, l.box[3] - l.box[1])
    cx, cy = (l.box[0] + l.box[2]) / 2, (l.box[1] + l.box[3]) / 2
    return [o for o in lines if o is not l
            and abs((o.box[1] + o.box[3]) / 2 - cy - ((o.box[0] + o.box[2]) / 2 - cx) * skew) < tol * h]


def _label_of(segment: str) -> str:
    """Classify the words just before a number: exclude | net | tax | words | total | weak | ''."""
    s = _fold(segment)
    s = s.rstrip("()（）[]$＄.,-－_|/")
    if not s:
        return ""
    if EXCLUDE_LABEL.search(s):
        return "exclude"
    if WORDS_LABEL.search(s):
        return "words"
    if NET_LABEL.search(s):
        return "net"
    if TAX_LABEL.search(s):
        return "tax"
    if TOTAL_LABEL.search(s):
        return "total"
    if WEAK_TOTAL_LABEL.search(s):
        return "weak"
    return "other" if re.search(r"[一-鿿A-Za-z]", s) else ""


def amount_tokens(lines: list[Line], payment_slip: bool = False, skew: float = 0.0) -> list[dict]:
    """Every number on the page that could be an amount, with the label it is printed under.
    Dates, times, phone numbers, ids, quantities with units and numbers of 7+ digits are dropped."""
    out = []
    for l in lines:
        t = re.sub(r"(\d),\s+(\d{3})(?!\d)", r"\1,\2", _INV.sub(lambda m: "#" * len(m.group(0)), l.text))
        prev_end, line_label = 0, None
        for m in _NUM_TOK.finditer(t):
            s, a, b = m.group(0), m.start(), m.end()
            pre, pre2 = t[a - 1:a], t[a - 2:a - 1]
            post, post2 = t[b:b + 1], t[b + 1:b + 2]
            post_w = t[b:].lstrip()[:1]
            seg = t[prev_end:a]
            prev_end = b
            raw_digits = re.sub(r"\D", "", s)
            if pre in "-/:#." and pre and pre2.isdigit() or post in "-/:." and post and post2.isdigit():
                continue                       # date, time, id with separators
            if pre == "#" or (pre.isascii() and pre.isalpha() and pre not in "T$") or post == "#":
                continue                       # glued to letters: an id
            if post_w and (post_w in _UNIT_AFTER or (post.isascii() and post.isalpha() and post not in "TX")):
                continue                       # quantity, unit, size
            if len(raw_digits) >= 7 and "," not in s and "." not in s:
                continue                       # UBN, serial, card or phone number
            v = int(raw_digits) if ("." in s and re.fullmatch(r"\d{1,3}(\.\d{3})+", s)) or "," in s else (
                int(s) if s.isdigit() else None)
            if v is None:
                continue
            lab = _label_of(seg)
            if lab in ("", "other") and line_label is None and not re.search(r"[一-鿿A-Za-z]", _fold(t[:a])):
                # number alone on its OCR line: the label is the nearest text to its left on the same row
                left = sorted((o for o in _row(lines, l, skew=skew) if o.box[2] <= l.box[0] + 8), key=lambda o: -o.box[2])
                for o in left:
                    ot = _INV.sub(" ", o.text)
                    tail = re.split(r"\d[\d,.]*", ot)[-1]
                    if re.search(r"\d", ot) and not re.search(r"[一-鿿A-Za-z]", tail):
                        break                  # the text to the left ends in its own number: no label for this one
                    lab2 = _label_of(tail)
                    if lab2:
                        lab = lab2
                        break
            if lab in ("total", "weak", "net", "tax", "exclude", "words"):
                line_label = lab
            elif line_label in ("exclude",) and lab in ("", "other") and not seg.strip():
                lab = "exclude"               # second number on a payment row (e.g. 現金 1,000 找零 166)
            if payment_slip and PAYMENT_SLIP_PAID.search(_fold(seg)):
                lab = "total"                  # on a payment slip the amount paid is the amount
            out.append({"v": v, "label": lab, "line": l, "pos": a})
    return out


def _arith_triples(vals: set[int]) -> list[tuple[int, int, int]]:
    out = []
    for n in vals:
        if n < 20:
            continue
        for t in vals:
            if 1 <= t < n and abs(t - n * 0.05) <= 1.01 and (n + t) in vals:
                out.append((n, t, n + t))
    return out


def header_slot(lines: list[Line], toks: list[dict], skew: float = 0.0) -> tuple[int | None, set]:
    """E-invoice proof: the total is printed on the 隨機碼 row. Returns the one other number on that row (and the row)."""
    for l in lines:
        f = _fold(l.text)
        if not re.search(r"隨機?碼|機碼|碼[:：]\d{4}", f):
            continue
        row = {id(l)} | {id(o) for o in _row(lines, l, skew=skew)}
        rnd = re.search(r"碼[:：]?(\d{4})", f)
        cands = {x["v"] for x in toks if id(x["line"]) in row and x["label"] not in ("exclude", "net", "tax", "words")}
        if rnd:
            cands.discard(int(rnd.group(1)))
        cands = {v for v in cands if v >= 1}
        if len(cands) == 1:
            return cands.pop(), row
    return None, set()


def amount_evidence(lines: list[Line], dtype: str | None, payment_slip: bool = False) -> dict:
    skew = estimate_skew(lines)
    toks = amount_tokens(lines, payment_slip, skew)
    ok = [x for x in toks if x["label"] != "exclude"]
    vals = {x["v"] for x in ok}
    tri = _arith_triples(vals)
    totals = {T for _, _, T in tri}
    # several triples: item prices can add up by chance (105 + 5 = 110); the totals block ends in the largest amount
    top_t = max(totals) if totals and max(totals) == max(vals) else (next(iter(totals)) if len(totals) == 1 else None)
    arith = next((x for x in tri if x[2] == top_t), None) if top_t is not None else None
    lab = Counter(x["v"] for x in ok if x["label"] == "total" and x["v"] > 0)
    label = None
    if lab:
        top = lab.most_common()
        label = top[0][0] if len(top) == 1 or top[0][1] > top[1][1] else None
    weak = sorted({x["v"] for x in ok if x["label"] == "weak" and x["v"] > 0})
    words = None
    for l in lines:
        w = cn_words_to_int(l.text)
        if w:
            words = w
    page = _fold(" ".join(l.text for l in lines))
    slot, slot_row = header_slot(lines, toks, skew) if dtype == "e_invoice" else (None, set())
    weak = weak[0] if len(weak) == 1 else None
    # the 總計 on the header row is one printed number, not two pieces of evidence: count it as the slot only
    if slot is not None:
        def only_on_slot_row(kind, v):
            return all(id(x["line"]) in slot_row for x in ok if x["label"] == kind and x["v"] == v)
        if label == slot and only_on_slot_row("total", slot):
            label = None
        if weak == slot and only_on_slot_row("weak", slot):
            weak = None
    return {
        "tokens": toks, "skew": skew, "on_page": vals, "excluded": {x["v"] for x in toks if x["label"] == "exclude"} - vals,
        "arith": arith, "arith_ambiguous": len(totals) > 1, "label": label,
        "label_conflict": bool(lab) and label is None, "weak": weak, "words": words, "slot": slot,
        "net_label": [x["v"] for x in ok if x["label"] == "net"], "tax_label": [x["v"] for x in ok if x["label"] == "tax"],
        "net_tax_printed": bool(NET_LABEL.search(page) or TAX_LABEL.search(page)),
    }


def decide_total(ev: dict, model_total: int | None, asked: int | None) -> tuple[int | None, str, bool, dict]:
    """Returns (total, provenance, needs_ask, detail).

    Sources: an arithmetic triple ending in the total, the e-invoice header slot, a total-labelled row, the amount in
    words, a 小計 row, the model's transcription and its answer to the total question. The model's numbers count only
    when the number is printed on the page outside a payment row. A total is accepted when at least two sources agree
    on it and no other value has as many; otherwise code asks the total question once, and if that does not settle
    it the total is marked unconfirmed (a person checks it)."""
    if isinstance(asked, str):
        asked = parse_ask(asked)
    src = {k: ev[k] for k in ("arith", "slot", "label", "words", "weak") if ev.get(k)}
    if src.get("arith"):
        src["arith"] = src["arith"][2]
    m = model_total if model_total and model_total in ev["on_page"] else None
    a = asked if asked and asked in ev["on_page"] else None
    if m:
        src["model"] = m
    if a:
        src["asked"] = a
    detail = {"sources": dict(src), "model": model_total, "asked": asked}
    rejected = sorted({v for v in (model_total, asked) if v and v in ev["excluded"]})
    if rejected:
        detail["rejected_payment_row"] = rejected
    support = Counter(src.values())
    ranked = support.most_common()
    if ranked:
        best, n = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0
        if n >= 2 and n > second:
            names = sorted(k for k, v in src.items() if v == best)
            how = "model-agree-on-page" if set(names) <= {"model", "asked"} else "ocr-" + "+".join(names)
            return best, how, False, detail
        guess = best if n > second else None
    else:
        guess = None
    return guess, "unconfirmed", asked is None, detail


def decide_net_tax(ev: dict, total: int | None, dtype: str | None, net_m: int | None, tax_m: int | None):
    """net / tax: an arithmetic triple that ends in the chosen total wins; then labelled rows; when the page prints no
    net or tax line (plain receipts, consumer e-invoices) both are empty."""
    tri = ev.get("arith")
    if tri and tri[2] == total:
        return tri[0], tri[1], "ocr-arith", "ocr-arith"
    nl, tl = ev["net_label"], ev["tax_label"]
    if nl or tl:
        net = nl[-1] if nl else net_m
        tax = tl[-1] if tl else tax_m
        return net, tax, ("ocr-label" if nl else "model-only"), ("ocr-label" if tl else "model-only")
    if not ev["net_tax_printed"] and dtype != "triplicate_invoice":
        return None, None, "not-printed", "not-printed"
    return net_m, tax_m, ("model-only" if net_m is not None else "missing"), ("model-only" if tax_m is not None else "missing")


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
        if extra.get("total_needs_ask"):
            ask = (raw.get("total_ask") or {}).get(TOTAL_ASK_KEY)
            if ask is None:
                ask = self.ask_total(path, lines)
                raw.setdefault("total_ask", {})[TOTAL_ASK_KEY] = ask
                raw["_dirty"] = True
            timing["ask_s"] = ask["vlm_s"]
            fields, prov, boxes, extra = postprocess(obj, lines, asked=ask["answer"])
        if prov.get("buyer_ubn") == "needs-zoom":
            img = Image.open(path).convert("RGB")
            v, how, box, cands, zt = self.zoom_vote(img, buyer_label_line(lines), extra.get("buyer_ubn_model"))
            timing["zoom_s"] = zt
            fields["buyer_ubn"], prov["buyer_ubn"] = v, how
            extra["buyer_ubn_reads"] = cands
            if box:
                boxes["buyer_ubn"] = box
        timing["total_s"] = round(sum(timing.get(k, 0) for k in ("ocr_s", "vlm_s", "zoom_s", "ask_s")), 2)
        return {
            "fields": fields, "provenance": prov, "boxes": boxes, "extra": extra,
            "ocr_lines": raw["ocr_lines"], "raw": raw["raw"], "parsed": bool(obj),
            "timing": timing, "model": raw.get("model", self.model_id), "device": raw.get("device", self.device),
            "image_size": raw["image_size"],
        }

    def ask_total(self, path: str | Path, lines: list[Line]) -> dict:
        """One short question: the total only. The answer is a vote; code decides whether it counts."""
        img = Image.open(path).convert("RGB")
        txt, t = self.generate(TOTAL_ASK_PROMPT.format(ocr=_fmt_ocr(lines)), img, max_new_tokens=12)
        return {"answer": txt.strip()[:40], "vlm_s": t["vlm_s"]}

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


def parse_ask(answer) -> int | None:
    """The model's answer to the total question: one number, else None."""
    if answer is None:
        return None
    nums = [N.to_int(x) for x in re.findall(r"\d{1,3}(?:,\d{3})+|\d+", str(answer))]
    nums = [x for x in nums if x]
    return nums[0] if len(nums) == 1 else None


def postprocess(obj: dict, lines: list[Line], asked: str | None = None) -> tuple[dict, dict, dict, dict]:
    """asked: the model's answer to TOTAL_ASK_PROMPT, when code asked for it (extra['total_needs_ask'])."""
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

    total_m, net_m, tax_m = N.to_int(obj.get("total")), N.to_int(obj.get("net")), N.to_int(obj.get("tax"))
    from .rules import voucher_kind
    slip = voucher_kind(" ".join(l.text for l in lines), inv) == "payment_slip"
    ev = amount_evidence(lines, dtype, payment_slip=slip)
    total, prov["total"], needs_ask, detail = decide_total(ev, total_m or None, parse_ask(asked))
    net, tax, prov["net"], prov["tax"] = decide_net_tax(ev, total, dtype, net_m, tax_m)
    extra["total_needs_ask"] = needs_ask
    extra["total_evidence"] = detail
    if asked is not None:
        extra["total_asked_raw"] = asked
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
