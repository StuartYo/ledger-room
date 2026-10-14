"""Turn what the model printed into typed values. Pure code, no model calls."""
from __future__ import annotations

import json
import re
from datetime import date

_DATE = re.compile(r"(\d{2,4})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})")
_PERIOD = re.compile(r"(\d{2,4})\s*年\s*(\d{1,2})\s*[-~～至－]\s*(\d{1,2})\s*月")


def parse_date(s) -> date | None:
    if not s:
        return None
    m = _DATE.search(str(s))
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if y < 1911:
        y += 1911          # ROC (民國) year
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_period(s) -> str | None:
    """'115年09-10月' -> '2026-09/2026-10'. Returns None when absent or malformed."""
    if not s:
        return None
    m = _PERIOD.search(str(s))
    if not m:
        return None
    y, a, b = (int(x) for x in m.groups())
    if y < 1911:
        y += 1911
    if not (1 <= a <= 12 and b == a + 1):
        return None
    return f"{y}-{a:02d}/{y}-{b:02d}"


def to_int(v) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    s = re.sub(r"[^\d.\-]", "", str(v))
    if not s or s in "-.":
        return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def digits(v) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\D", "", str(v))
    return s or None


def clean_str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in ("", "null", "none", "n/a") else s


def parse_json_loose(text: str) -> dict | None:
    """Parse the first JSON object in a model reply, repairing common small-model slips."""
    if not text:
        return None
    t = re.sub(r"```(?:json)?", "", text)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None
    t = t[a:b + 1]
    for attempt in range(3):
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            if attempt == 0:
                # 2,360 after a colon -> 2360 ; trailing commas
                t = re.sub(r'(:\s*)(\d{1,3}(?:,\d{3})+)(?=\s*[,}\]])', lambda m: m.group(1) + m.group(2).replace(",", ""), t)
                t = re.sub(r",\s*([}\]])", r"\1", t)
            elif attempt == 1:
                t = t.replace("None", "null").replace("True", "true").replace("False", "false")
    return None
