"""Text-line OCR (PP-OCRv4 det/rec models shipped in the rapidocr_onnxruntime wheel),
executed by the OpenVINO runtime instead of ONNX Runtime.

On the dev laptop (i7-13620H, onnxruntime 1.31) the stock ONNX Runtime path took
17-28 s per page; the same .onnx files compiled by OpenVINO run in 0.3-1 s.
OCR gives us exact digit strings and line boxes (for evidence crops); the
vision-language model gives us structure and meaning.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import cv2  # noqa: F401  loaded before OpenVINO: with opencv-python 5.0 the other order crashes cvtColor on Windows
import numpy as np
import openvino as ov

_lock = threading.Lock()
_engine = None
_core = ov.Core()


@dataclass
class Line:
    id: str
    text: str
    box: tuple[int, int, int, int]   # x0, y0, x1, y1 in original image pixels
    conf: float


class _OVSession:
    """Drop-in for rapidocr_onnxruntime.utils.OrtInferSession backed by OpenVINO."""
    device = os.environ.get("LEDGER_OCR_DEVICE", "CPU")

    def __init__(self, config):
        import onnxruntime as ort  # only used to read the model's metadata (character list)
        path = str(config["model_path"])
        self._meta = ort.InferenceSession(path, providers=["CPUExecutionProvider"]).get_modelmeta().custom_metadata_map
        n = os.environ.get("LEDGER_CPU_THREADS")   # optional cap, e.g. on a shared office PC
        cfg = {"INFERENCE_NUM_THREADS": int(n)} if n and self.device == "CPU" else {}
        self.cm = _core.compile_model(_core.read_model(path), self.device, cfg)
        self.req = self.cm.create_infer_request()

    def __call__(self, x):
        r = self.req.infer({0: x})
        return [r[o] for o in self.cm.outputs]

    def get_character_list(self, key="character"):
        return self._meta[key].splitlines()

    def have_key(self, key="character"):
        return key in self._meta


def _get():
    global _engine
    with _lock:
        if _engine is None:
            import rapidocr_onnxruntime.ch_ppocr_cls.text_cls as tc
            import rapidocr_onnxruntime.ch_ppocr_det.text_detect as td
            import rapidocr_onnxruntime.ch_ppocr_rec.text_recognize as tr
            from rapidocr_onnxruntime import RapidOCR
            for mod in (td, tc, tr):
                mod.OrtInferSession = _OVSession
            _engine = RapidOCR()
    return _engine


def read_lines(image) -> list[Line]:
    """image: path or RGB numpy array."""
    eng = _get()
    if not isinstance(image, (str, os.PathLike)):
        image = np.asarray(image)[:, :, ::-1].copy()   # RapidOCR expects BGR arrays
    with _lock:
        res, _ = eng(str(image) if isinstance(image, os.PathLike) else image)
    out = []
    for i, (box, txt, conf) in enumerate(res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append(Line(f"L{i + 1}", txt, (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))), float(conf)))
    return out


def recognise_only(rgb: np.ndarray) -> str:
    """Run only the recognition model on an already-cropped RGB image (no detection)."""
    eng = _get()
    with _lock:
        res, _ = eng(np.asarray(rgb)[:, :, ::-1].copy(), use_det=False, use_cls=False, use_rec=True)
    return res[0][0] if res else ""
