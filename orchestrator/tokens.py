"""API tokens: how a script, another machine or a bot reaches the orchestrator's Tasks API.

A token is `orch_ID_SECRET`, shown once when it is made. The orchestrator
keeps only its ID, name, who made it, and a SHA-256 of the whole token, in
tokens.json under its state directory (readable only by its user), so the file
gives nothing away. Sent as `Authorization: Bearer orch_...`, a token reaches
/api/tasks and nothing else: not sessions, jobs, or the tokens themselves,
which are made and revoked only by someone signed in, or on this machine.

A token needs neither the Host check nor the X-Orchestrator header. Those stop
a web page from using the browser's way in; a browser never sends a token of
its own accord, so a page cannot borrow one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PREFIX = "orch_"
TOKEN = re.compile(r"orch_([0-9a-f]{8})_[A-Za-z0-9_-]{20,64}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,47}")
# last_used is written at most this often per token, so a busy client does not rewrite the file each request.
TOUCH_EVERY = 60


class TokenError(ValueError):
    """A token that cannot be made or revoked as asked."""


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            records = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        return records if isinstance(records, list) else []

    def _save(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_suffix(".part")
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(records, handle, indent=2)
        part.replace(self.path)

    def tokens(self) -> list[dict[str, Any]]:
        """Every token, without its hash."""
        with self.lock:
            return [_public(r) for r in self._load()]

    def create(self, name: str, *, user: str = "") -> tuple[dict[str, Any], str]:
        """A new token: its record, and the token itself, which is not kept and cannot be shown again."""
        name = name.strip()
        if not NAME.fullmatch(name):
            raise TokenError("Name the token (what uses it): letters, digits, spaces, . _ -, up to 48.")
        with self.lock:
            records = self._load()
            if any(r["name"] == name for r in records):
                raise TokenError(f"There is already a token named {name!r}: revoke it, or pick another name.")
            ident = secrets.token_hex(4)
            while any(r["id"] == ident for r in records):
                ident = secrets.token_hex(4)
            token = f"{PREFIX}{ident}_{secrets.token_urlsafe(32)}"
            record = {"id": ident, "name": name, "created": _now(), "created_by": user, "last_used": None,
                      "hash": _digest(token)}
            self._save([*records, record])
        return _public(record), token

    def revoke(self, ident: str) -> dict[str, Any]:
        with self.lock:
            records = self._load()
            found = next((r for r in records if r["id"] == ident), None)
            if found is None:
                raise TokenError(f"No token {ident!r}.")
            self._save([r for r in records if r["id"] != ident])
        return _public(found)

    def check(self, token: str) -> dict[str, Any] | None:
        """The record of a valid token, or None."""
        match = TOKEN.fullmatch(token.strip())
        if not match:
            return None
        digest = _digest(token.strip())
        with self.lock:
            records = self._load()
            found = next((r for r in records if r["id"] == match.group(1)), None)
            if found is None or not hmac.compare_digest(found["hash"], digest):
                return None
            if time.time() - found.get("touched", 0) > TOUCH_EVERY:
                found.update(last_used=_now(), touched=time.time())
                self._save(records)
        return _public(found)


def user_of(record: dict[str, Any]) -> str:
    """Who a request with this token is from, as a task's created_by shows it."""
    return f"{record['created_by']} via {record['name']}" if record.get("created_by") else f"token {record['name']}"


def _public(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in ("hash", "touched")}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
