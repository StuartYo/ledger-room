#!/usr/bin/env bash
# Ledger Room: prepare a Linux host with an Intel Arc Pro B70 for the Stage II measurement.
#
#   bash scripts/setup_host.sh                      # check GPU, make .venv, install, telemetry off, models, tests
#   bash scripts/setup_host.sh --apt                # also apt-install system packages (needs sudo; Ubuntu/Debian)
#   bash scripts/setup_host.sh --models-root /data/models --skip-models
#
# Written for Ubuntu 24.04 / 26.04. Not yet run on the contest host (its OS was unknown when this was written).
# Then: .venv/bin/python -m eval.bench_b70   and   .venv/bin/python -m eval.offline_check
set -euo pipefail

APT=0
SKIP_MODELS=0
KEEP_TELEMETRY=0
MODELS_ROOT="${LEDGER_MODELS_ROOT:-$HOME/models}"
while [ $# -gt 0 ]; do
  case "$1" in
    --apt) APT=1 ;;
    --skip-models) SKIP_MODELS=1 ;;
    --keep-telemetry) KEEP_TELEMETRY=1 ;;
    --models-root) MODELS_ROOT="$2"; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
  shift
done

cd "$(dirname "$0")/.."
[ -f requirements.txt ] || { echo "run from the ledger-room repository"; exit 1; }
step() { printf '\n== %s\n' "$*"; }

step "1/8 system packages"
if [ "$APT" = 1 ]; then
  # python venv, OpenCV runtime libs for the OCR wheel, OpenCL / Level Zero GPU runtime (Intel compute runtime)
  sudo apt-get update
  sudo apt-get install -y python3-venv python3-pip libgl1 libglib2.0-0 clinfo \
    intel-opencl-icd libze1 libze-intel-gpu1 || {
      echo "Some GPU runtime packages were not found in this release's archive."
      echo "Install Intel's compute runtime from https://github.com/intel/compute-runtime/releases and re-run."; }
  sudo usermod -aG render,video "$USER" || true
else
  echo "skipped (add --apt to install python3-venv libgl1 libglib2.0-0 clinfo intel-opencl-icd libze1 libze-intel-gpu1)"
fi

step "2/8 GPU visible to the OS"
ls -l /dev/dri/renderD* 2>/dev/null || echo "WARNING: no /dev/dri/renderD* node: the GPU driver (xe / i915) is not loaded"
id -nG | tr ' ' '\n' | grep -qx render || echo "WARNING: $USER is not in the 'render' group (log out and in after --apt)"
if command -v clinfo >/dev/null; then clinfo -l || true; else echo "clinfo not installed"; fi
lspci 2>/dev/null | grep -Ei 'vga|display|3d' || true

step "3/8 Python"
PY=""
for c in python3.12 python3.13 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "need Python 3.10+ (sudo apt-get install python3.12 python3.12-venv)"; exit 1; }
echo "using $PY ($($PY --version))"

step "4/8 virtual environment and packages"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

step "5/8 OpenVINO telemetry off"
# `import openvino` loads openvino.tools.ovc, which sends a usage event to Google Analytics unless this consent
# file says 0 (found 2026-11-02). Ledger Room promises that nothing leaves the machine at run time.
if [ "$KEEP_TELEMETRY" = 1 ]; then
  echo "skipped (--keep-telemetry)"
else
  .venv/bin/opt_in_out --opt_out
  echo "consent file: $HOME/intel/openvino_telemetry = $(cat "$HOME/intel/openvino_telemetry" 2>/dev/null || echo missing)"
fi

step "6/8 inventory (OpenVINO devices, memory, disk)"
mkdir -p "$MODELS_ROOT"
.venv/bin/python -m eval.bench_b70 --inventory-only --models-root "$MODELS_ROOT"

step "7/8 models (OpenVINO INT4, pre-converted by Intel; 9B 5.85 GB, 27B 14.87 GB)"
if [ "$SKIP_MODELS" = 1 ]; then
  echo "skipped"
else
  FREE_GB=$(df -BG --output=avail "$MODELS_ROOT" | tail -1 | tr -dc '0-9')
  echo "free at $MODELS_ROOT: ${FREE_GB} GB (need about 25 GB for both)"
  for repo in OpenVINO/Qwen3.5-9B-int4-ov OpenVINO/Qwen3.5-27B-int4-ov; do
    d="$MODELS_ROOT/${repo#OpenVINO/}"
    if [ -f "$d/openvino_language_model.xml" ]; then echo "present: $d"; continue; fi
    .venv/bin/python -m ledger_room.get_model --repo "$repo" --dir "$d"   # HF_TOKEN from the environment if needed
  done
fi

step "8/8 tests (no model needed)"
.venv/bin/python -m pytest -q

cat <<EOF

Done. Next:
  export LEDGER_MODELS_ROOT="$MODELS_ROOT"
  .venv/bin/python -m eval.bench_b70          # writes eval/results_b70.json
  .venv/bin/python -m eval.offline_check      # writes eval/results_offline.json
EOF
