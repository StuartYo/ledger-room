"""Purge when a client's contract ends (safeguard regulation Art. 19): delete working copies, keep the audit logs,
write the deletion record (what, when, who, method, hashes) to the operation log; dry run changes nothing."""
import csv
import hashlib
import io
import json

import pytest

from ledger_room import privacy
from ledger_room.pipeline import AuditLog

KEY = b"k" * 32


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def firm(tmp_path):
    data = tmp_path / "data"
    out = data / "out"
    imgs = {}
    for cid in ("C01", "C02"):
        for k in (1, 2):
            p = data / "main" / "clients" / cid / "2026-09" / f"IMG_{k}.jpg"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(f"{cid}-{k}-jpeg".encode())
            imgs[f"{cid}/2026-09/IMG_{k}.jpg"] = _sha(p.read_bytes())
    clients = [{"id": "C01", "name": "青嵐室內設計有限公司", "ubn": "91541552"},
               {"id": "C02", "name": "港町咖啡有限公司", "ubn": "85215306"}]
    (data / "main" / "clients.json").write_text(json.dumps({"clients": clients}, ensure_ascii=False), encoding="utf-8")
    # another inbox that also has a C01 (a different firm's test set): must not be touched
    (data / "other" / "clients" / "C01" / "2026-09").mkdir(parents=True)
    (data / "other" / "clients" / "C01" / "2026-09" / "IMG_1.jpg").write_bytes(b"other-C01")
    (data / "other" / "clients.json").write_text(json.dumps({"clients": [{"id": "C01", "name": "x", "ubn": "1"}]}),
                                                 encoding="utf-8")
    # cached reads, keyed by the image hash
    (out / "cache").mkdir(parents=True)
    for doc, h in imgs.items():
        (out / "cache" / f"{h[:24]}_abcdef123456.json").write_text(json.dumps({"fields": {"seller_name": "張志豪"}}),
                                                                    encoding="utf-8")
        (out / "cache" / f"{h[:24]}_abcdef123456_raw.json").write_text("{}", encoding="utf-8")
    # a run over the whole inbox
    run = out / "runs" / "main-r1"
    (run / "crops").mkdir(parents=True)
    docs = [{"doc_id": d, "client_id": d[:3], "image": f"main/clients/{d}", "fields": {}} for d in imgs]
    exs = [{"doc_id": d, "client_id": d[:3], "rule_id": "OVER_LIMIT", "image": f"main/clients/{d}",
            "evidence": [{"field": "total", "crop": f"runs/main-r1/crops/{d[:3]}_2026-09_IMG_1_total.png"}]}
           for d in imgs if d.endswith("IMG_1.jpg")]
    for e in exs:
        (out / e["evidence"][0]["crop"]).write_bytes(b"png")
    res = {"summary": {"run_id": "main-r1", "set": "main", "n_docs": 4, "n_exceptions": 2,
                       "by_rule": {"OVER_LIMIT": 2}, "by_client": {"C01": 1, "C02": 1}},
           "clients": clients, "docs": docs, "exceptions": exs}
    (run / "result.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["client_id", "client_ubn", "doc_id", "total"])
    w.writeheader()
    for d in imgs:
        w.writerow({"client_id": d[:3], "client_ubn": "x", "doc_id": d, "total": 1})
    (run / "export_media.csv").write_text("﻿" + buf.getvalue(), encoding="utf-8")
    (run / "91541552.TXT").write_text("media file for C01", encoding="utf-8")
    log = AuditLog(run / "audit_log.jsonl", key=KEY)
    for d, h in imgs.items():
        log.add("doc_read", doc_id=d, sha256=h)
    # an image copy kept by a benchmark
    (out / "bench" / "subset" / "C01").mkdir(parents=True)
    (out / "bench" / "subset" / "C01" / "copy.jpg").write_bytes(b"C01-1-jpeg")
    # an evaluation file that names a C01 document: listed for review, not deleted
    (out / "baseline").mkdir()
    (out / "baseline" / "eval.jsonl").write_text(json.dumps({"image": "main/clients/C01/2026-09/IMG_2.jpg"}) + "\n",
                                                 encoding="utf-8")
    return data, imgs


def test_dry_run_changes_nothing_and_lists_everything(firm):
    data, imgs = firm
    before = _snapshot(data)
    plan = privacy.purge("C01", reason="contract ended", set_name="main", data_dir=data, dry_run=True, key=KEY)
    assert _snapshot(data) == before
    paths = {d["path"]: d for d in plan["delete"]}
    assert {"main/clients/C01/2026-09/IMG_1.jpg", "main/clients/C01/2026-09/IMG_2.jpg"} <= set(paths)
    assert paths["main/clients/C01/2026-09/IMG_1.jpg"]["sha256"] == imgs["C01/2026-09/IMG_1.jpg"]
    assert sum(1 for p in paths if p.startswith("out/cache/")) == 4
    assert "out/runs/main-r1/crops/C01_2026-09_IMG_1_total.png" in paths
    assert "out/runs/main-r1/91541552.TXT" in paths and "out/bench/subset/C01/copy.jpg" in paths
    assert not any("C02" in p or p.startswith("other/") for p in paths)
    assert {r["path"] for r in plan["rewrite"]} == {"main/clients.json", "out/runs/main-r1/result.json",
                                                     "out/runs/main-r1/export_media.csv"}
    assert [k["path"] for k in plan["kept"]] == ["out/runs/main-r1/audit_log.jsonl"]
    assert [r["path"] for r in plan["review"]] == ["out/baseline/eval.jsonl"]


def test_purge_deletes_client_keeps_others_and_writes_deletion_record(firm):
    data, imgs = firm
    out = data / "out"
    audit_before = (out / "runs" / "main-r1" / "audit_log.jsonl").read_bytes()
    r = privacy.purge("C01", reason="contract ended 2026-10-31", set_name="main", operator="amy", data_dir=data, key=KEY)
    assert r["errors"] == []
    assert not (data / "main" / "clients" / "C01").exists()
    assert (data / "main" / "clients" / "C02" / "2026-09" / "IMG_1.jpg").exists()
    assert (data / "other" / "clients" / "C01" / "2026-09" / "IMG_1.jpg").exists()
    cache = sorted(p.name[:24] for p in (out / "cache").iterdir())
    assert cache == sorted([imgs["C02/2026-09/IMG_1.jpg"][:24], imgs["C02/2026-09/IMG_2.jpg"][:24]] * 2)
    assert not (out / "bench" / "subset" / "C01" / "copy.jpg").exists()
    assert not (out / "runs" / "main-r1" / "91541552.TXT").exists()
    assert sorted(p.name for p in (out / "runs" / "main-r1" / "crops").iterdir()) == ["C02_2026-09_IMG_1_total.png"]
    res = json.loads((out / "runs" / "main-r1" / "result.json").read_text(encoding="utf-8"))
    assert {d["client_id"] for d in res["docs"]} == {"C02"} and {e["client_id"] for e in res["exceptions"]} == {"C02"}
    assert [c["id"] for c in res["clients"]] == ["C02"] and res["summary"]["by_client"] == {"C02": 1}
    assert res["summary"]["n_docs"] == 2 and res["purged_clients"] == ["C01"]
    rows = list(csv.DictReader(io.StringIO((out / "runs" / "main-r1" / "export_media.csv").read_text(encoding="utf-8-sig"))))
    assert {x["client_id"] for x in rows} == {"C02"} and len(rows) == 2
    cfg = json.loads((data / "main" / "clients.json").read_text(encoding="utf-8"))
    assert [c["id"] for c in cfg["clients"]] == ["C02"]
    # kept: the run's audit log is untouched (Art. 19, five years); the evaluation file is left for review
    assert (out / "runs" / "main-r1" / "audit_log.jsonl").read_bytes() == audit_before
    assert (out / "baseline" / "eval.jsonl").exists()
    # the deletion record: what, when, who, method, hashes, in the HMAC-chained operation log
    log = out / "oplog.jsonl"
    assert AuditLog.verify(log, KEY)[0]
    es = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert es[0]["event"] == "purge_start" and es[-1]["event"] == "purge_end"
    s = es[0]
    assert s["user"] == "amy" and s["reason"] == "contract ended 2026-10-31" and s["client_id"] == "C01"
    assert "os.remove" in s["method"] and s["ts"] and "flno=19" in s["basis"]["url"]
    deleted = {e["path"]: e for e in es if e["event"] == "purge_file" and e["action"] == "deleted"}
    assert deleted["main/clients/C01/2026-09/IMG_2.jpg"]["sha256"] == imgs["C01/2026-09/IMG_2.jpg"]
    assert len(deleted) == r["done"]["files_deleted"] == len(r["delete"])
    rewritten = [e for e in es if e["event"] == "purge_file" and e["action"] == "rewritten"]
    assert len(rewritten) == 3 and all(e["sha256_before"] != e["sha256_after"] for e in rewritten)
    assert es[-1]["kept"] == ["out/runs/main-r1/audit_log.jsonl"] and es[-1]["left_for_review"] == ["out/baseline/eval.jsonl"]


def test_ambiguous_or_bad_ids_and_missing_reason_are_refused(firm):
    data, _ = firm
    with pytest.raises(privacy.PurgeError, match="several inboxes"):
        privacy.plan_purge("C01", None, data)
    assert privacy.plan_purge("C02", None, data)["set"] == "main"
    for bad in ("../main", "C01/..", "", "C 01"):
        with pytest.raises(privacy.PurgeError):
            privacy.plan_purge(bad, "main", data)
    with pytest.raises(privacy.PurgeError, match="not in inbox"):
        privacy.plan_purge("C09", "main", data)
    with pytest.raises(privacy.PurgeError, match="reason"):
        privacy.purge("C02", reason="  ", set_name="main", data_dir=data, key=KEY)


def test_cli_dry_run(firm, capsys):
    data, _ = firm
    before = _snapshot(data)
    rc = privacy.main(["purge", "--client", "C02", "--set", "main", "--reason", "contract ended", "--dry-run",
                       "--data", str(data)])
    assert rc == 0 and _snapshot(data) == before
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["client_id"] == "C02" and out["totals"]["files_deleted"] > 0
    assert privacy.main(["purge", "--client", "C01", "--reason", "x", "--dry-run", "--data", str(data)]) == 2
