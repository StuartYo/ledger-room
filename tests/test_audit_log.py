"""The audit log's HMAC catches an edit that keeps the plain SHA-256 chain consistent."""
import hashlib
import json

from ledger_room.pipeline import AuditLog

KEY = b"k" * 32


def test_hmac_detects_rechained_edit(tmp_path):
    p = tmp_path / "log.jsonl"
    log = AuditLog(p, key=KEY)
    for i in range(3):
        log.add("doc_read", doc_id=f"d{i}")
    assert AuditLog.verify_detail(p, KEY) == {"chain_ok": True, "mac_ok": True, "n": 3}
    # someone without the key edits entry 2 and recomputes every SHA-256 so the plain chain still links up
    es = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    es[1]["doc_id"] = "forged"
    prev = "0" * 64
    for e in es:
        e.pop("hash")
        e.pop("mac")
        e["prev"] = prev
        e["hash"] = prev = hashlib.sha256(json.dumps(e, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        e["mac"] = "0" * 64
    p.write_text("".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in es), encoding="utf-8")
    d = AuditLog.verify_detail(p, KEY)
    assert d["chain_ok"] is True and d["mac_ok"] is False      # the plain chain is fooled, the HMAC is not


def test_append_mode_continues_the_chain(tmp_path):
    p = tmp_path / "op.jsonl"
    AuditLog(p, key=KEY, append=True).add("login", user="a")
    AuditLog(p, key=KEY, append=True).add("logout", user="a")
    assert AuditLog.verify_detail(p, KEY)["n"] == 2 and AuditLog.verify(p, KEY)[0]
