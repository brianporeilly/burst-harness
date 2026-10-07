"""`harness egress`: see what a gateway refused, and approve it.

A gateway in `allowlist` mode refuses at once and records the refusal with an
id; nothing waits. Approving is a separate act, by whoever runs the sandbox:

    harness egress pending                 what running gateways have refused
    harness egress approve ID              let that destination through
        --scope once | session | 2h | permanent      (default: session)
    harness egress allow HOST[:PORT]       allow it for good, without an id
    harness egress revoke HOST[:PORT]      take that back
    harness egress list                    what is allowed for good

Scopes: `once` is one connection; `session` lasts until that gateway stops (so,
for a `harness session`, until it is next stopped); a duration (`90m`, `2h`,
`1d`) expires; `permanent` is also written to `state_dir`/egress/approved.json,
which every allowlist includes from then on.

An approval reaches a running gateway as a line in its approvals file, which it
re-reads when the file changes. Nothing is restarted, and an approval only ever
adds to the allowlist.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

from . import containers
from .config import HarnessConfig

APPROVALS_FILE = f"{containers.GATEWAY_CA_DIR}/approvals.json"
# Refusals of a destination; a refused `request` is about a Host header, not somewhere to allow.
DESTINATION_KINDS = ("connect", "http", "tls", "tcp", "dns")
HOST = re.compile(r"[A-Za-z0-9*]([A-Za-z0-9*.-]*[A-Za-z0-9*])?(:\d{1,5})?")
DURATION = re.compile(r"(\d+)([smhd]?)")
UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


class EgressError(RuntimeError):
    """Nothing matched, or a gateway could not be read or written."""


def gateways() -> list[dict[str, str]]:
    """Running gateway sidecars the harness started."""
    return [c for c in containers.list_containers()
            if c["name"].endswith("-egress") and c["status"].lower().startswith(("running", "up"))]


def events(gateway: dict[str, str]) -> list[dict[str, Any]]:
    proc = subprocess.run([gateway["binary"], "logs", gateway["name"]], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    found = []
    for line in proc.stdout.splitlines():
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


def destination(event: dict[str, Any]) -> str:
    """An allowlist entry for what an event was refused: the host, with its port unless that is 443."""
    port = event.get("port")
    return str(event["host"]) if port in (None, 443) else f"{event['host']}:{port}"


def read_approvals(gateway: dict[str, str]) -> list[dict[str, Any]]:
    proc = subprocess.run([gateway["binary"], "exec", gateway["name"], "cat", APPROVALS_FILE], capture_output=True, text=True)
    try:
        return list(json.loads(proc.stdout).get("approvals", [])) if proc.returncode == 0 and proc.stdout.strip() else []
    except (ValueError, AttributeError):
        return []


def write_approvals(gateway: dict[str, str], approvals: list[dict[str, Any]]) -> None:
    # Written beside the file and moved over it, so the gateway never reads half of one.
    command = f"cat > {APPROVALS_FILE}.new && mv {APPROVALS_FILE}.new {APPROVALS_FILE}"
    proc = subprocess.run([gateway["binary"], "exec", "-i", gateway["name"], "sh", "-c", command],
                          input=json.dumps({"approvals": approvals}, indent=2) + "\n", capture_output=True, text=True)
    if proc.returncode != 0:
        raise EgressError(f"Could not write approvals to {gateway['name']}: {proc.stderr.strip()}")


def pending() -> list[dict[str, Any]]:
    """Destinations running gateways have refused and nobody has approved, newest refusal of each."""
    rows = []
    for gateway in gateways():
        approved = {str(a.get("allow")) for a in read_approvals(gateway)}
        latest: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {}
        for event in events(gateway):
            if event.get("verdict") == "deny" and event.get("kind") in DESTINATION_KINDS and event.get("host") not in (None, "-"):
                where = destination(event)
                latest[where], counts[where] = event, counts.get(where, 0) + 1
        for where, event in latest.items():
            if where not in approved:
                rows.append({"id": event["id"], "destination": where, "times": counts[where], "last": event["ts"],
                             "sandbox": event.get("sandbox") or gateway["name"].removesuffix("-egress")})
    return sorted(rows, key=lambda row: row["last"])


def _scope(scope: str) -> dict[str, Any]:
    """The part of an approval entry a scope stands for."""
    scope = scope.strip().lower()
    if scope == "once":
        return {"once": True}
    if scope in ("session", "permanent"):
        return {}
    if not (match := DURATION.fullmatch(scope)) or not int(match[1]):
        raise EgressError(f"Not a scope: {scope!r}. Use once, session, permanent, or a duration such as 90m, 2h, 1d.")
    return {"expires": int(time.time()) + int(match[1]) * UNITS[match[2]]}


def approve(cfg: HarnessConfig, request_id: str, scope: str = "session") -> str:
    """Approve what the refusal `request_id` was for, on the gateway that refused it."""
    extra = _scope(scope)
    for gateway in gateways():
        event = next((e for e in events(gateway) if e.get("id") == request_id and e.get("verdict") == "deny"), None)
        if event is None:
            continue
        if event.get("kind") not in DESTINATION_KINDS or event.get("host") in (None, "-"):
            raise EgressError(f"{request_id} was not refused for where it was going ({event.get('detail') or event.get('kind')}).")
        where = destination(event)
        kept = [a for a in read_approvals(gateway) if a.get("id") != request_id]
        write_approvals(gateway, [*kept, {"allow": where, "id": request_id, **extra}])
        sandbox = gateway["name"].removesuffix("-egress")
        if scope.strip().lower() == "permanent":
            allow(cfg, where)
            return f"Approved {where} for {sandbox}, and for every allowlist from now on."
        return f"Approved {where} for {sandbox} ({scope})."
    raise EgressError(
        f"No running gateway has a refusal with id {request_id}. If its sandbox has stopped, "
        "`harness egress allow HOST` allows a host from the next start."
    )


def _check(host: str) -> str:
    host = host.strip().lower()
    if not HOST.fullmatch(host):
        raise EgressError(f"Not a host: {host!r}. Use example.com, *.example.com, or host:port.")
    return host


def _save(cfg: HarnessConfig, hosts: list[str]) -> None:
    path = containers.approved_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"allow": hosts}, indent=2) + "\n")


def allow(cfg: HarnessConfig, host: str) -> str:
    """Allow a host for good: part of every allowlist from the next gateway started."""
    host = _check(host)
    hosts = containers.approved(cfg)
    if host in hosts:
        return f"{host} is already allowed."
    _save(cfg, [*hosts, host])
    return f"Allowed {host} from now on (gateways already running are not changed; approve a refusal for those)."


def revoke(cfg: HarnessConfig, host: str) -> str:
    """Take back an approval of a host: from the permanent list, and from running gateways."""
    host = _check(host)
    hosts = containers.approved(cfg)
    removed = host in hosts
    if removed:
        _save(cfg, [h for h in hosts if h != host])
    live = 0
    for gateway in gateways():
        approvals = read_approvals(gateway)
        kept = [a for a in approvals if str(a.get("allow")) != host]
        if len(kept) != len(approvals):
            write_approvals(gateway, kept)
            live += 1
    if not removed and not live:
        return f"{host} was not approved anywhere. (A host in egress_allow is configuration, not an approval.)"
    return f"Revoked {host}" + (" from the permanent list" if removed else "") + (f" and {live} running gateway(s)" if live else "") + "."
