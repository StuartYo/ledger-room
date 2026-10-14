"""Office user accounts for the Ledger Room web app (stored outside the repository and the data folder).

python -m web.users add amy --role admin    # asks for a password twice
python -m web.users add ben --role staff
python -m web.users list
python -m web.users remove ben

Roles (the safeguard regulation, Art. 15, asks for different permissions per person):
  admin  everything: run the model live, read the audit log and the operation log, export
  staff  read exceptions and documents, replay cached runs, export the 401 CSV

Passwords are stored as PBKDF2-SHA256 (200,000 rounds, random salt). File: <secrets dir>/users.json (see keys.py),
or LEDGER_USERS.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger_room.keys import secrets_dir  # noqa: E402

ROLES = ("admin", "staff")
ROUNDS = 200_000


def users_path() -> Path:
    p = os.environ.get("LEDGER_USERS")
    return Path(p) if p else secrets_dir() / "users.json"


def load() -> dict:
    p = users_path()
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("users", {})


def save(users: dict) -> None:
    p = users_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"users": users}, ensure_ascii=False, indent=1), encoding="utf-8")


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ROUNDS).hex()


def make_user(password: str, role: str) -> dict:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if len(password) < 10:
        raise ValueError("use at least 10 characters")
    salt = secrets.token_bytes(16)
    return {"role": role, "salt": salt.hex(), "hash": _hash(password, salt)}


def check(users: dict, name: str, password: str) -> str | None:
    """The user's role when the password is right, else None."""
    u = users.get(name)
    if not u:
        _hash(password, b"0" * 16)      # same work either way, so timing does not reveal valid names
        return None
    ok = hmac.compare_digest(_hash(password, bytes.fromhex(u["salt"])), u["hash"])
    return u["role"] if ok else None


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--role", choices=ROLES, default="staff")
    r = sub.add_parser("remove")
    r.add_argument("name")
    sub.add_parser("list")
    args = ap.parse_args()
    users = load()
    if args.cmd == "list":
        for n, u in sorted(users.items()):
            print(f"{n}\t{u['role']}")
        print(f"({users_path()})")
    elif args.cmd == "remove":
        users.pop(args.name, None)
        save(users)
        print("removed", args.name)
    else:
        pw = getpass.getpass(f"password for {args.name}: ")
        if pw != getpass.getpass("again: "):
            sys.exit("passwords differ")
        users[args.name] = make_user(pw, args.role)
        save(users)
        print(f"saved {args.name} ({args.role}) to {users_path()}")


if __name__ == "__main__":
    main()
