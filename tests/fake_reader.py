"""A stand-in reader for tests/test_bench_concurrency.py (no model, no GPU). Loaded in spawned worker processes as
"tests.fake_reader:FakeReader"; model_dir is the path of a JSON config:
  sleep_s        seconds per document
  crash_once     file names: the worker process dies (os._exit) the first time it reads one (marker file in marker_dir)
  crash_always   file names: the worker process dies every time
  error_always   file names: a Python exception every time
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class FakeReader:
    def __init__(self, model_dir: str, device: str):
        self.cfg = json.loads(Path(model_dir).read_text(encoding="utf-8"))
        self.model_id, self.device, self.thinking = "fake-reader", device, "off"

    def read_raw(self, path) -> dict:
        name = Path(path).name
        c = self.cfg
        time.sleep(c.get("sleep_s", 0.05))
        if name in c.get("crash_always", []):
            os._exit(134)
        if name in c.get("crash_once", []):
            marker = Path(c["marker_dir"]) / f"{name}.crashed"
            if not marker.exists():
                marker.write_text("x", encoding="utf-8")
                os._exit(134)
        if name in c.get("error_always", []):
            raise ValueError(f"fake read error on {name}")
        return {"ocr_lines": [], "raw": "", "timing": {"total_s": c.get("sleep_s", 0.05)}, "image_size": [8, 8],
                "model": self.model_id, "device": self.device}

    def read(self, path, raw=None) -> dict:
        raw = raw or self.read_raw(path)
        return {"fields": {"doc_type": "receipt", "total": 100, "items": []}, "provenance": {"total": "ocr"},
                "boxes": {}, "timing": raw["timing"], "image_size": raw["image_size"], "ocr_lines": [],
                "model": self.model_id, "device": self.device}
