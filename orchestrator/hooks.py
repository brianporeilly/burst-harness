"""Webhooks: a forge says when something changed, so the orchestrator looks now rather than at its next poll.

A forge (GitHub, or one of `forges`: Forgejo) posts to /hooks/NAME, NAME being the forge's name ("github",
"forgejo"...). Each delivery is signed with a secret shared with the forge, the credential
<NAME>_WEBHOOK_SECRET (Settings): HMAC-SHA256 of the body, in X-Hub-Signature-256 (GitHub, as sha256=HEX) or
X-Forgejo-Signature / X-Gitea-Signature (Forgejo, HEX). One unsigned, wrongly signed, or for a forge with no
secret set is refused. A delivery seen before (its delivery id) is not acted on again.

What a delivery says is a hint, never taken as true: it names what to look at, and the orchestrator looks as
its polling would, through the forge's API or git.
  pull_request, closed    the tasks whose PR it is look at their PR now (tasks.check_pr): a merged one is
                          `merged`, and with auto_continue starts its next round.
  push                    the job repos (sources.py) at that repo and branch sync now.
  ping                    nothing (the forge checking the hook).
Anything else is recorded and ignored. Polling goes on beside them, for a delivery that never came.

On a cluster the orchestrator's own port answers only Envoy, whose sign-in it trusts, so a forge inside the
cluster is given a port of its own, ORCHESTRATOR_HOOKS_PORT, that answers /hooks/ and nothing else
(app.py). A forge outside reaches /hooks/NAME on the orchestrator's address, through a route that passes that
path alone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from harness import credentials, forge
from harness.config import HarnessConfig

PATH = "/hooks/"
PORT_ENV = "ORCHESTRATOR_HOOKS_PORT"
MAX_BODY = 1024 * 1024  # GitHub's are capped at 25 MB, but what is acted on here is small
KEPT = 20  # deliveries listed per forge
SEEN = 500  # delivery ids remembered per forge, so a redelivery is not acted on twice


class HookError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.reason = message  # what the forge is answered: a line of ours, never an exception's own text


def secret_var(name: str) -> str:
    """GITHUB_WEBHOOK_SECRET for the forge named github."""
    return re.sub(r"[^A-Z0-9_]", "_", name.upper()) + "_WEBHOOK_SECRET"


def _norm(url: str) -> str:
    """A repo's or a PR's URL, for comparing: lowercase, without a trailing / or .git."""
    return url.strip().lower().rstrip("/").removesuffix(".git")


class Hooks:
    def __init__(self, base: HarnessConfig, *, tasks: Any = None, sources: Any = None, port: int = 0,
                 background: bool = True) -> None:
        self.base, self.tasks, self.sources, self.port = base, tasks, sources, port
        self.background = background  # what a delivery asks is done in a thread, so the forge is answered now
        self.lock = threading.Lock()
        self.deliveries: dict[str, deque[dict[str, Any]]] = {}
        self.seen: dict[str, deque[str]] = {}

    def forges(self) -> dict[str, forge.Forge]:
        try:
            return {f.name: f for f in forge.forges(self.base)}
        except forge.ForgeError:
            return {"github": forge.GITHUB}

    # --- a delivery ---

    def receive(self, name: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        """Check a delivery and act on it; what was done. Raises HookError (with the status to answer)."""
        headers = {k.lower(): v for k, v in headers.items()}
        found = self.forges().get(name)
        if found is None:
            raise HookError(404, f"No forge named {name}.")
        event = (headers.get("x-github-event") if found.kind == "github" else
                 headers.get("x-forgejo-event") or headers.get("x-gitea-event") or headers.get("x-github-event")) or ""
        delivery = (headers.get("x-github-delivery") if found.kind == "github" else
                    headers.get("x-forgejo-delivery") or headers.get("x-gitea-delivery")
                    or headers.get("x-github-delivery")) or ""
        try:
            self._verify(found, headers, body)
        except HookError as err:
            self._record(name, event, delivery, f"refused: {err}")
            raise
        with self.lock:
            seen = self.seen.setdefault(name, deque(maxlen=SEEN))
            if delivery and delivery in seen:
                self._record(name, event, delivery, "a redelivery: done already", locked=True)
                return {"did": "nothing: delivered before"}
            if delivery:
                seen.append(delivery)
        try:
            payload = json.loads(body)
        except ValueError as err:
            self._record(name, event, delivery, "refused: the body is not JSON")
            raise HookError(400, "The body is not JSON (set the hook's content type to application/json).") from err
        if not isinstance(payload, dict):
            raise HookError(400, "The body is not a JSON object.")
        did, work = self._plan(found, event, payload)
        self._record(name, event, delivery, did)
        if work:
            if self.background:
                threading.Thread(target=self._do, args=(name, work), name="hook", daemon=True).start()
            else:
                self._do(name, work)
        return {"did": did}

    def _verify(self, found: forge.Forge, headers: dict[str, str], body: bytes) -> None:
        secret = credentials.get(secret_var(found.name))
        if not secret:
            raise HookError(403, f"{secret_var(found.name)} is not set (Settings, Credentials): no delivery is "
                                 "taken without it.")
        if len(body) > MAX_BODY:
            raise HookError(413, "The delivery is too large.")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        given = [headers.get("x-hub-signature-256", "").removeprefix("sha256=")]
        if found.kind != "github":
            given += [headers.get("x-forgejo-signature", ""), headers.get("x-gitea-signature", "")]
        if not any(sig and hmac.compare_digest(sig.strip().lower(), expected) for sig in given):
            raise HookError(401, "The signature does not match: is the hook's secret the one set here?")

    def _plan(self, found: forge.Forge, event: str, payload: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
        """What the delivery asks to look at: a line saying so, and [(kind, what)]."""
        repo = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
        where = str(repo.get("html_url") or "")
        if event == "ping":
            return "ping: nothing to do", []
        if where and forge._host(where).lower() != found.host:
            return f"ignored: {where} is not on {found.host}", []
        if event == "pull_request":
            pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
            url = str(pr.get("html_url") or "")
            if payload.get("action") != "closed" or not url:
                return f"pull_request {payload.get('action')}: nothing to do", []
            slugs = self.tasks.by_pr(url) if self.tasks is not None else []
            if not slugs:
                return f"pull_request closed: no task's PR is {url}", []
            return f"pull_request closed: looking at the PR of {', '.join(slugs)}", [("task", s) for s in slugs]
        if event == "push":
            ref = str(payload.get("ref") or "")
            if not ref.startswith("refs/heads/"):
                return f"push to {ref}: not a branch", []
            branch = ref.removeprefix("refs/heads/")
            urls = {_norm(str(repo.get(k) or "")) for k in ("html_url", "clone_url")} - {""}
            names = [name for name, source in (self.sources.sources().items() if self.sources is not None else [])
                     if _norm(source.url) in urls and (source.branch or repo.get("default_branch")) == branch]
            if not names:
                return f"push to {branch}: no job repo follows it", []
            return f"push to {branch}: syncing job repo {', '.join(names)}", [("source", n) for n in names]
        return f"{event or 'an event'}: not one acted on", []

    def _do(self, name: str, work: list[tuple[str, str]]) -> None:
        for kind, what in work:
            try:
                if kind == "task":
                    self.tasks.check_pr(what, 0)
                else:
                    self.sources.sync(what)
            except Exception as err:  # noqa: BLE001 -- the poll tries again
                print(f"hooks: {name}: {kind} {what}: {type(err).__name__}: {err}", flush=True)

    def _record(self, name: str, event: str, delivery: str, outcome: str, *, locked: bool = False) -> None:
        entry = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, "delivery": delivery,
                 "outcome": outcome, "time": time.time()}
        if locked:
            self.deliveries.setdefault(name, deque(maxlen=KEPT)).appendleft(entry)
            return
        with self.lock:
            self.deliveries.setdefault(name, deque(maxlen=KEPT)).appendleft(entry)

    # --- for the page ---

    def listing(self) -> dict[str, Any]:
        with self.lock:
            kept = {name: list(found) for name, found in self.deliveries.items()}
        rows = [{"name": name, "kind": found.kind, "host": found.host, "path": PATH + name,
                 "secret": secret_var(name), "secret_set": bool(credentials.get(secret_var(name))),
                 "deliveries": [{k: v for k, v in d.items() if k != "time"} for d in kept.get(name, [])]}
                for name, found in self.forges().items()]
        return {"forges": rows, "port": self.port}
