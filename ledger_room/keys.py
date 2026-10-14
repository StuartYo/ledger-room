"""Operator secrets kept outside the repository and outside the data folder.

audit.key: 32 random bytes used to HMAC every audit-log entry. Without it, anyone who can write the data disk could
edit an entry and recompute the plain SHA-256 chain. With it, such an edit is detectable by whoever holds the key.
It does not protect against someone who holds both the disk and the key, so for real use keep the key on a separate
USB key or under a separate Windows account and point LEDGER_AUDIT_KEY_FILE at it.

Default location: %LOCALAPPDATA%\\LedgerRoom\\ (Windows) or ~/.ledger_room/ (elsewhere), or LEDGER_SECRETS_DIR.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def secrets_dir() -> Path:
    d = os.environ.get("LEDGER_SECRETS_DIR")
    if d:
        return Path(d)
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "LedgerRoom" if base else Path.home() / ".ledger_room"


def audit_key_path() -> Path:
    p = os.environ.get("LEDGER_AUDIT_KEY_FILE")
    return Path(p) if p else secrets_dir() / "audit.key"


def audit_key(create: bool = True) -> bytes | None:
    """The operator's HMAC key; created (32 random bytes) on first use unless create=False."""
    p = audit_key_path()
    if p.exists():
        k = p.read_bytes()
        if len(k) < 16:
            raise ValueError(f"{p} is too short to be an audit key")
        return k
    if not create:
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    k = secrets.token_bytes(32)
    p.write_bytes(k)
    return k
