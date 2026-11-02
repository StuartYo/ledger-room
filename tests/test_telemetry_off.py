"""OpenVINO telemetry stays off by Ledger Room's own default (ledger_room/__init__.py), with no machine setting."""
import json
import re
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = r"""
import json, sys, time
calls = []
def hook(event, args):
    if event in ("socket.connect", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname"):
        calls.append([event, repr(args[1] if event.startswith("socket.connect") or event == "socket.sendto" else args[0])])
sys.addaudithook(hook)
import ledger_room.ocr   # imports openvino, which imports openvino.tools.ovc and its telemetry
time.sleep(2)            # the telemetry sender runs in a thread pool
import openvino_telemetry as tm
t = tm.Telemetry._SingletonMetaClass__single_instance
print(json.dumps({"calls": calls, "consent": getattr(t, "consent", None)}))
"""


def test_import_sends_nothing_without_ci_in_the_environment():
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "TF_BUILD", "JENKINS_URL")}
    r = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["calls"] == [], out["calls"]
    assert out["consent"] is False


def test_flag_is_set_before_openvino_can_be_imported():
    src = (ROOT / "ledger_room" / "__init__.py").read_text(encoding="utf-8")
    assert 'os.environ["CI"] = "true"' in src
    before = src.split('os.environ["CI"] = "true"')[0]
    assert not re.search(r"^\s*(import|from)\s+(openvino|\.)", before, re.M)
