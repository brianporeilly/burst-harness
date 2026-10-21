"""A cluster's shared egress gateway, from the driver's side (ROADMAP phase 6).

One gateway serves every sandbox in a namespace, and Calico lets sandbox pods
reach nothing else. It refuses an address nobody registered, so after claiming
a sandbox the driver registers the pod's IP with what that sandbox may do --
its allowlist, the credentials the gateway adds for it, read-only and its
exceptions -- and unregisters it on release, saving its events the way a
podman gateway's are saved. See the top of gateway/burst_gateway.py for the
API.

The settings are the driver's: `gateway_control` (the API's URL, say
http://burst-gateway.agent-sandbox-system.svc:8081) and the token, from
HARNESS_GATEWAY_TOKEN or the file `gateway_token_file`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import HarnessConfig

TOKEN_ENV = "HARNESS_GATEWAY_TOKEN"


class GatewayError(RuntimeError):
    """The gateway could not be reached, or refused what it was sent."""


@dataclass(frozen=True)
class Gateway:
    url: str
    token: str = field(repr=False)

    @classmethod
    def from_config(cls, cfg: HarnessConfig) -> "Gateway | None":
        """The configured gateway, or None when sandboxes have none."""
        if not cfg.gateway_control:
            return None
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token and cfg.gateway_token_file:
            try:
                token = Path(cfg.gateway_token_file).expanduser().read_text().strip()
            except OSError as err:
                raise GatewayError(f"gateway_token_file: {err}") from err
        if not token:
            raise GatewayError(f"gateway_control is set, but there is no token: set {TOKEN_ENV} or gateway_token_file.")
        return cls(url=cfg.gateway_control.rstrip("/"), token=token)

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as err:
            try:
                detail = json.loads(err.read()).get("message") or err.reason
            except ValueError:
                detail = err.reason
            raise GatewayError(f"gateway {method} {path}: {err.code} {detail}") from err
        except (OSError, ValueError) as err:
            raise GatewayError(f"gateway at {self.url} could not be reached: {err}") from err

    def register(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("PUT", f"/sandboxes/{name}", body)

    def unregister(self, name: str) -> list[dict[str, Any]]:
        """Forget the sandbox; its events, all those the gateway still kept."""
        return list(self._call("DELETE", f"/sandboxes/{name}").get("events") or [])

    def events(self, name: str, after: int = 0) -> dict[str, Any]:
        return self._call("GET", f"/sandboxes/{name}/events?after={after}")


def registration(
    cfg: HarnessConfig, *, address: str, ttl: int, allow: tuple[str, ...] = (),
    inject: tuple[dict[str, object], ...] = (), read_only: tuple[dict[str, object], ...] | None = None,
    upstream_ca: tuple[dict[str, str], ...] = (),
) -> tuple[dict[str, Any], str]:
    """What to register a sandbox with, built as start_egress builds a podman gateway's
    settings, and a line saying what it amounts to (never the credentials)."""
    from . import containers  # containers imports agent_sandbox, which imports this

    body: dict[str, Any] = {"address": address, "ttl": ttl}
    if cfg.egress == "allowlist":
        hosts = containers.allowlist(cfg, allow)
        body.update(mode="allowlist", allow=hosts)
        summary = f"allowlist: {', '.join(hosts)}"
    else:
        # With a gateway in the way there is no unrecorded "open": everything passes, and is recorded.
        containers.check_reachable(cfg)
        body.update(mode="log")
        summary = "everything passes and is recorded"
    if rules := [*containers.inject_rules(cfg), *inject]:
        body["inject"] = rules
        summary += "; adds credentials for " + ", ".join(sorted({str(rule["host"]) for rule in rules}))
    if upstream_ca:
        body["upstream_ca"] = list(upstream_ca)
    if read_only is not None:
        body.update(readonly=True, writes=list(read_only))
        summary += "; read-only" + (" except " + ", ".join(f"{w['method']} {w['host']}{w.get('path', '')}"
                                                         for w in read_only) if read_only else "")
    return body, summary


def save_events(path: str, events: list[dict[str, Any]]) -> str:
    """Write a sandbox's events where a podman gateway's would go. Returns a line saying so."""
    from .exec_target import refused_summary

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(event) + "\n" for event in events).encode()
    target.write_bytes(data)
    return f" Egress events: {target}{refused_summary(data)}"
