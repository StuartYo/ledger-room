"""Every number shown in README.md and eval/RESULTS.md must come from eval/results.json."""
import re
from pathlib import Path

import pytest

from eval import report

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def R():
    p = ROOT / "eval" / "results.json"
    if not p.exists():
        pytest.skip("no results yet")
    return report.load()


def test_readme_block_matches_json(R):
    txt = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(re.escape(report.START) + r".*?" + re.escape(report.END), txt, flags=re.S)
    assert m, "README results block missing"
    assert m.group(0) == report.readme_block(R), "README numbers differ from eval/results.json; run python -m eval.report"


def test_results_md_matches_json(R):
    assert (ROOT / "eval" / "RESULTS.md").read_text(encoding="utf-8") == report.results_md(R)


def test_results_flag_synthetic(R):
    assert R["synthetic"] is True
    assert "B70" in R["hardware"] and "plans" in R["hardware"]


def test_oracle_control_is_perfect(R):
    for s, v in R["sets"].items():
        assert v["oracle_fields"]["f1"] == 1.0, s
