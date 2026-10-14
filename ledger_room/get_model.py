"""One-time model download (the only step that needs the internet).

python -m ledger_room.get_model                      # -> E:\models\Qwen3.5-2B-int4-ov (or $LEDGER_MODEL_DIR)
python -m ledger_room.get_model --repo OpenVINO/Qwen3.5-9B-int4-ov --dir D:\models\Qwen3.5-9B-int4-ov
"""
import argparse
import os

from huggingface_hub import snapshot_download

ap = argparse.ArgumentParser()
ap.add_argument("--repo", default="OpenVINO/Qwen3.5-2B-int4-ov")
ap.add_argument("--dir", default=os.environ.get("LEDGER_MODEL_DIR", r"E:\models\Qwen3.5-2B-int4-ov"))
a = ap.parse_args()
print(snapshot_download(a.repo, local_dir=a.dir))
