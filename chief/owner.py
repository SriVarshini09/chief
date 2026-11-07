"""The one person this Chief works for: a name, an email and a password, kept in the state dir.

Chief is single-owner today (one web token, one Telegram owner, one Composio user id), so there is
exactly one account, stored in `<state_dir>/owner.json`. The password is never stored: only a salted
`hashlib.scrypt` hash. If this ever grows to several people, this file becomes one row per person.

Forgot the password? Delete `owner.json` and create the account again (that needs the web token or a
browser on the computer running Chief; see docs/sign-in.md).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

OWNER_FILE = "owner.json"
MIN_PASSWORD = 10
MAX_PASSWORD = 256  # scrypt's cost grows with input; nobody types more
MAX_NAME = 80
MAX_EMAIL = 254
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}  # about 16 MB and a few tens of ms per check
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OwnerError(ValueError):
    """Something the person typed cannot be used. The message is safe to show them."""


def hash_password(password: str, salt: bytes | None = None, params: dict | None = None) -> dict:
    salt = salt or secrets.token_bytes(16)
    p = dict(params or SCRYPT)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=p["n"], r=p["r"], p=p["p"], dklen=p["dklen"],
                            maxmem=128 * p["r"] * p["n"] * 2)
    return {"algo": "scrypt", **p, "salt": salt.hex(), "hash": digest.hex()}


def check_password(password: str, stored: dict) -> bool:
    try:
        if stored.get("algo") != "scrypt":
            return False
        params = {k: int(stored[k]) for k in ("n", "r", "p", "dklen")}
        fresh = hash_password(password, bytes.fromhex(stored["salt"]), params)
        return hmac.compare_digest(fresh["hash"], str(stored["hash"]))
    except (KeyError, TypeError, ValueError):
        return False


_DUMMY = hash_password("not anybody's password", b"\0" * 16)


@dataclass
class Owner:
    name: str
    email: str
    created: str

    def public(self) -> dict:
        return {"name": self.name, "email": self.email}


class OwnerStore:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / OWNER_FILE

    def _read(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("email") and isinstance(data.get("password"), dict) else None

    def exists(self) -> bool:
        return self._read() is not None

    def get(self) -> Owner | None:
        data = self._read()
        return Owner(str(data.get("name", "")), str(data["email"]), str(data.get("created", ""))) if data else None

    def create(self, name: str, email: str, password: str) -> Owner:
        name = " ".join(str(name or "").split())[:MAX_NAME]
        email = str(email or "").strip().lower()
        password = str(password or "")
        if self.exists():
            raise OwnerError("This Chief already has an account. Sign in instead.")
        if not name:
            raise OwnerError("Tell Chief your name.")
        if len(email) > MAX_EMAIL or not _EMAIL.match(email):
            raise OwnerError("That doesn't look like an email address.")
        if len(password) < MIN_PASSWORD:
            raise OwnerError(f"Use a password of at least {MIN_PASSWORD} characters.")
        if len(password) > MAX_PASSWORD:
            raise OwnerError(f"Use a password of at most {MAX_PASSWORD} characters.")
        owner = Owner(name, email, datetime.now().isoformat(timespec="seconds"))
        record = {"name": owner.name, "email": owner.email, "created": owner.created, "password": hash_password(password)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # only you can read it (no effect on Windows, where the user folder is private)
        except OSError:
            pass
        os.replace(tmp, self.path)
        return owner

    def verify(self, email: str, password: str) -> Owner | None:
        """The owner if the email and password match, else None. Always does one scrypt, so a wrong
        email takes as long as a wrong password and the timing tells nothing."""
        data = self._read()
        email = str(email or "").strip().lower()
        password = str(password or "")[:MAX_PASSWORD]
        if not data:
            check_password(password, _DUMMY)
            return None
        ok = check_password(password, data["password"])
        if not (ok and hmac.compare_digest(email.encode(), str(data["email"]).encode())):
            return None
        return Owner(str(data.get("name", "")), str(data["email"]), str(data.get("created", "")))
