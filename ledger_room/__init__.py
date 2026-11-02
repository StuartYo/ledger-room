"""Ledger Room.

OpenVINO telemetry is switched off here, before any module of this package imports openvino.

Why: `import openvino` also imports the model-conversion tool `openvino.tools.ovc` (openvino/__init__.py,
"Model Conversion API - ovc should reside in the main namespace"), and `openvino/tools/ovc/__init__.py` then sends an
anonymous `ov / import` event to Google Analytics 4 through the `openvino_telemetry` package. With no consent file on
the machine it sends (found by eval/offline_check.py on 2026-11-02).

How: OpenVINO documents one switch, `opt_in_out --opt_out`, which writes "0" to a per-user consent file
(https://docs.openvino.ai/2026/about-openvino/additional-resources/telemetry.html). That changes the machine, so Ledger
Room does not rely on it. Instead it uses the in-process switch in the same official code: ovc initialises telemetry
with `disable_in_ci=True` (openvino/tools/ovc/telemetry_utils.py, init_ovc_telemetry), and openvino_telemetry treats
the environment variable CI=true as "declined" (openvino_telemetry/utils/opt_in_checker.py, OptInChecker._run_in_ci;
https://github.com/openvinotoolkit/telemetry). Setting CI=true for this process, and for any child process it starts,
means no telemetry event is created. Checked by eval/offline_check.py (eval/results_offline.json).
"""
from __future__ import annotations

import os
import sys
import warnings

OPENVINO_TELEMETRY_OFF = "CI=true set by ledger_room before importing openvino"

os.environ["CI"] = "true"

if "openvino" in sys.modules:   # someone imported openvino before ledger_room: its import event may already be gone
    warnings.warn("openvino was imported before ledger_room, so OpenVINO telemetry may already have sent its import "
                  "event; import ledger_room first", RuntimeWarning, stacklevel=2)
