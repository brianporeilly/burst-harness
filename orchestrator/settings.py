"""Settings: credentials set in the orchestrator's page instead of its environment.

A credential set here (the Settings tab) is used in place of the environment
variable of the same name by everything that reads credentials
(harness/credentials.py): sessions' gateways, tasks' and jobs' runs, and the
page's own checks. Clearing it goes back to the environment's, if there is one.
It takes effect for what starts next; a run already going keeps what it had.

Values are write-only. The page and the API say whether a credential is set,
where its value comes from, and who set it when, never the value itself.

They are kept encrypted (Fernet: AES-128-CBC with HMAC-SHA256) in
credentials.enc under the state directory, readable only by the orchestrator's
user. The key is ORCHESTRATOR_SECRETS_KEY, or else one the orchestrator makes
in secrets.key beside it. That one keeps the values out of anything that takes
credentials.enc without it; set ORCHESTRATOR_SECRETS_KEY (on a cluster, from a
Secret) to keep the key away from the data altogether. A key that does not open
an existing credentials.enc stops the orchestrator from starting, rather than
starting without the credentials or writing over them.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from harness import credentials

KEY_ENV = "ORCHESTRATOR_SECRETS_KEY"
NAME = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
MAX_VALUE = 16_384
# Offered in the page whether or not they have a value anywhere, in this order.
SUGGESTED = ("HARNESS_GIT_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY",
             "SLACK_BOT_TOKEN", "SLACK_WEBHOOK_URL")
USES = {
    "HARNESS_GIT_TOKEN": "Clones, pushes and pull requests (GH_TOKEN and GITHUB_TOKEN also work).",
    "ANTHROPIC_API_KEY": "Anthropic's API, for provider anthropic and for Claude Code.",
    "CLAUDE_CODE_OAUTH_TOKEN": "Claude Code on a subscription (claude setup-token).",
    "OPENAI_API_KEY": "provider openai.",
    "SLACK_BOT_TOKEN": "Jobs that post to Slack.",
    "SLACK_WEBHOOK_URL": "Jobs that post to Slack through a webhook.",
}


class SettingsError(ValueError):
    """A credential that cannot be set or cleared as asked, or a store that cannot be opened."""


def load_key(state_dir: Path) -> tuple[bytes, str]:
    """The key, and a line saying where it came from."""
    if given := os.environ.get(KEY_ENV, "").strip():
        key, where = given.encode(), f"key from {KEY_ENV}"
    else:
        path = state_dir / "secrets.key"
        if not path.exists():
            state_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
        key, where = path.read_bytes().strip(), f"key in {path}, beside them (set {KEY_ENV} to keep it apart)"
    try:
        Fernet(key)
    except ValueError as err:
        raise SettingsError(f"{KEY_ENV} is not a Fernet key (32 bytes, url-safe base64): {err}") from err
    return key, where


class CredentialStore:
    def __init__(self, path: Path, key: bytes) -> None:
        self.path = path
        self.fernet = Fernet(key)
        self.lock = threading.Lock()
        credentials.use_stored(self._values(self._load()))

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            sealed = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            records = json.loads(self.fernet.decrypt(sealed))
        except InvalidToken as err:
            raise SettingsError(f"{self.path} does not open with this key: is {KEY_ENV} the one it was "
                                "written with? Nothing was changed.") from err
        return records if isinstance(records, dict) else {}

    def _save(self, records: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_suffix(".part")
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(self.fernet.encrypt(json.dumps(records).encode()))
        part.replace(self.path)
        credentials.use_stored(self._values(records))

    @staticmethod
    def _values(records: dict[str, dict[str, Any]]) -> dict[str, str]:
        return {name: str(record["value"]) for name, record in records.items()}

    def listing(self, names: Iterable[str] = ()) -> list[dict[str, Any]]:
        """The suggested credentials, these names, and every stored one: where each comes from, never its value."""
        with self.lock:
            records = self._load()
        rows = []
        for name in dict.fromkeys((*SUGGESTED, *names, *sorted(records))):
            record = records.get(name, {})
            rows.append({"name": name, "source": credentials.source(name), "use": USES.get(name, ""),
                         "updated": record.get("updated"), "updated_by": record.get("updated_by", ""),
                         # Set here and in the environment: clearing it here goes back to the environment's.
                         "environment_too": bool(record) and bool(os.environ.get(name, "").strip())})
        return rows

    def set(self, name: str, value: str, *, user: str = "") -> None:
        check_name(name)
        value = value.strip()
        if not value:
            raise SettingsError("Give a value. To go back to the environment's, clear it instead.")
        if len(value) > MAX_VALUE or any(c in value for c in "\r\n\0"):
            raise SettingsError(f"A value is one line, up to {MAX_VALUE:,} characters.")
        with self.lock:
            records = self._load()
            records[name] = {"value": value, "updated": _now(), "updated_by": user}
            self._save(records)

    def clear(self, name: str) -> bool:
        """Forget the stored value. False if there was none."""
        check_name(name)
        with self.lock:
            records = self._load()
            if records.pop(name, None) is None:
                return False
            self._save(records)
        return True


def check_name(name: str) -> None:
    if not NAME.fullmatch(name):
        raise SettingsError("A credential's name is an environment variable's: capitals, digits and _.")
    if not credentials.is_secret(name):
        raise SettingsError(f"{name} is not a credential (a name ending in _TOKEN, _KEY, _SECRET and so on, or one "
                            "the harness knows). Settings that are not secret stay in the configuration.")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
