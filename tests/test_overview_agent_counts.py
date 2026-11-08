"""The overview's agent numbers agree with each other (web/static/agent_counts.js, run in node).

The case behind this test is the heldout2 check of 2026-11-08: the agent checked 7 held vouchers and code verified 2,
but only 1 of them was cleared; the other still had a date outside the period. The strip showed 1 and the line under
it said "verified 2", with nothing saying where the second one went.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web" / "static"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

RUN = r"""
const fs = require("fs"), vm = require("vm");
const ctx = vm.createContext({ module: undefined });
for (const f of ["i18n.js", "agent_counts.js"]) vm.runInContext(fs.readFileSync(process.argv[1] + "/" + f, "utf8"), ctx);
const { I18N, agentSplit, agentMetaText } = vm.runInContext("({ I18N, agentSplit, agentMetaText })", ctx);
const inp = JSON.parse(fs.readFileSync(0, "utf8"));
const out = {};
for (const lang of ["zh", "en"]) {     // the same lookup as app.js t()
  const t = (k, v = {}) => ((I18N[lang] && I18N[lang][k]) ?? I18N.en[k] ?? k).replace(/\{(\w+)\}/g, (_, x) => (v[x] ?? ""));
  const sp = agentSplit(inp.docs, new Set(inp.held));
  out[lang] = { sp, text: agentMetaText(inp.A, sp, t), legend: t("lgAgent") };
}
process.stdout.write(JSON.stringify(out));
"""


def run(docs, held, A):
    p = subprocess.run([NODE, "-e", RUN, str(STATIC)], input=json.dumps({"docs": docs, "held": held, "A": A}),
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def doc(i, status=None, overridden=False):
    d = {"doc_id": f"J05/2026-09/IMG_{i}.jpg"}
    if status:
        d["agent"] = {"status": status, **({"overridden": True} if overridden else {})}
    return d


AGENT = {"ran": True, "model": "Qwen3.5-27B-int4-ov", "tried": 7, "verified": 2}


def test_heldout2_case_says_where_the_second_verified_voucher_went():
    docs = [doc(1, "verified"), doc(2, "verified")] + [doc(i, "held") for i in range(3, 8)] + [doc(9)]
    held = [d["doc_id"] for d in docs[1:8]]          # IMG_2 is verified but still held: date outside the period
    out = run(docs, held, AGENT)
    sp = out["zh"]["sp"]
    assert sp == {"verified": 2, "cleared": 1, "stillHeld": 1, "rejected": 0}
    assert sp["cleared"] + sp["stillHeld"] + sp["rejected"] == sp["verified"]
    assert out["zh"]["text"] == ("代理（Qwen3.5-27B-int4-ov）查了 7 張被擋的單據，程式驗過 2 張"
                                 "\n其中 1 張直接入帳，1 張還有別的問題要你決定")
    assert out["en"]["text"] == ("The agent (Qwen3.5-27B-int4-ov) checked 7 held vouchers; code verified 2"
                                 "\n1 cleared, 1 held for another reason (yours to decide)")
    # the strip's part is the cleared ones, and its label says so
    assert out["zh"]["legend"].endswith("直接入帳") and out["en"]["legend"].endswith("cleared")


def test_all_cleared_none_cleared_and_rejected():
    two = [doc(1, "verified"), doc(2, "verified"), doc(3, "held")]
    assert run(two, [two[2]["doc_id"]], AGENT)["zh"]["text"].endswith("程式驗過 2 張\n全部直接入帳")
    out = run(two, [d["doc_id"] for d in two], AGENT)
    assert out["zh"]["text"].endswith("程式驗過 2 張\n但都還有別的問題要你決定")
    rej = [doc(1, "verified", overridden=True), doc(2, "verified")]
    out = run(rej, [rej[0]["doc_id"]], AGENT)          # a rejected voucher goes back to the queue
    assert out["zh"]["sp"] == {"verified": 2, "cleared": 1, "stillHeld": 0, "rejected": 1}
    assert out["zh"]["text"].endswith("程式驗過 2 張\n全部直接入帳；1 張被人退回")


def test_nothing_verified_and_agent_not_run():
    docs = [doc(1, "held")]
    assert run(docs, [docs[0]["doc_id"]], AGENT)["zh"]["text"].endswith("程式驗過 0 張")
    assert run(docs, [], {"ran": True, "tried": 0})["zh"]["text"] == "這次沒有要代理看的單據"
    assert run(docs, [], {"ran": False})["zh"]["text"] == ""


def test_overview_uses_the_shared_count_and_labels_who_ran_the_check():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert html.index("static/agent_counts.js") < html.index("static/app.js")
    assert "agentSplit(S.run.docs, im)" in app and "agentMetaText(A, sp, t)" in app
    assert 'aline.split("\\n").map(esc).join("<br>")' in app      # the second line is a line break on the page
    # the operator name used to be appended bare ("... 3.8 秒・demo"): it is now labelled
    assert "esc(runUser())]" not in app and 'metaRunBy' in app
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    assert 'metaRunBy: "由 {u} 執行"' in i18n and 'metaRunBy: "run by {u}"' in i18n
