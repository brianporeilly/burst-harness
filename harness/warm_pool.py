"""Replace a warm pool's idle sandboxes that are out of date with their template (an image bump, say).

agent-sandbox's warm pool keeps its sandboxes ready to claim, but does not replace them when their
SandboxTemplate changes: after an image bump the next claim gets a sandbox still on the old image. `recycle`
finds the pool's idle sandboxes whose pod differs from the template's in what matters here (each container's
image, and its resources), and deletes them, and the pool makes new ones from the template as it stands.

Only an idle sandbox is touched: one whose controller is still its SandboxWarmPool, not a claim. It is deleted
with preconditions, its uid and resourceVersion as read, so one a claim takes in the meantime (which changes
it) is refused rather than deleted from under the claim. The orchestrator runs it every few minutes on a
cluster (orchestrator/__main__.py); `harness recycle-warm` runs it once.

It needs, in the pools' namespace: get on sandboxtemplates, and list and delete on sandboxes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .agent_sandbox import AgentSandboxError, _kubectl
from .config import HarnessConfig

SANDBOX_API = "agents.x-k8s.io/v1beta1"
TEMPLATE_REF = "agents.x-k8s.io/sandbox-template-ref"
WARM_POOL_KIND = "SandboxWarmPool"


@dataclass(frozen=True)
class Stale:
    name: str
    template: str
    uid: str
    version: str
    differs: str  # what is out of date, for saying so


def _pod(spec: dict[str, Any]) -> dict[str, Any]:
    """Of a pod template, what a sandbox has to match: each container's image and resources, by name."""
    pod = (spec.get("podTemplate") or {}).get("spec") or {}
    return {f"{kind} {c.get('name')}": {"image": c.get("image"), "resources": c.get("resources") or {}}
            for kind in ("initContainers", "containers") for c in pod.get(kind) or []}


def _differs(have: dict[str, Any], want: dict[str, Any]) -> str:
    for key in sorted({*have, *want}):
        if key not in have or key not in want:
            return f"{key.split(' ', 1)[1]} {'added' if key not in have else 'removed'}"
        if have[key]["image"] != want[key]["image"]:
            return f"image {_short(have[key]['image'])} -> {_short(want[key]['image'])}"
        if have[key]["resources"] != want[key]["resources"]:
            return f"{key.split(' ', 1)[1]}'s resources"
    return ""


def _short(image: Any) -> str:
    """repo:tag, without the registry's path or the digest."""
    return str(image).split("@", 1)[0].rsplit("/", 1)[-1]


def stale(cfg: HarnessConfig, pools: tuple[str, ...]) -> list[Stale]:
    """The idle sandboxes of these pools' templates that differ from their template now."""
    items = json.loads(_kubectl(cfg, "get", "sandboxes", "-o", "json") or "{}").get("items", [])
    templates: dict[str, dict[str, Any]] = {}
    found = []
    for item in items:
        meta = item.get("metadata") or {}
        owner = next((o for o in meta.get("ownerReferences") or [] if o.get("controller")), {})
        template = (meta.get("annotations") or {}).get(TEMPLATE_REF, "")
        if owner.get("kind") != WARM_POOL_KIND or template not in pools or meta.get("deletionTimestamp"):
            continue  # claimed, someone else's, or on its way out
        if template not in templates:
            templates[template] = _pod(json.loads(_kubectl(cfg, "get", "sandboxtemplate", template, "-o", "json")).get("spec") or {})
        if differs := _differs(_pod(item.get("spec") or {}), templates[template]):
            found.append(Stale(meta["name"], template, meta.get("uid", ""), meta.get("resourceVersion", ""), differs))
    return found


def recycle(cfg: HarnessConfig, pools: tuple[str, ...]) -> list[str]:
    """Delete the pools' idle sandboxes that are out of date; a line for each, said for the log."""
    if not cfg.namespace:
        raise AgentSandboxError("Recycling a warm pool needs its namespace (namespace).")
    done = []
    for old in stale(cfg, tuple(p for p in pools if p)):
        options = {"kind": "DeleteOptions", "apiVersion": "v1", "propagationPolicy": "Background",
                   "preconditions": {"uid": old.uid, "resourceVersion": old.version}}
        try:
            _kubectl(cfg, "delete", "--raw", f"/apis/{SANDBOX_API}/namespaces/{cfg.namespace}/sandboxes/{old.name}",
                     "-f", "-", stdin=json.dumps(options), namespaced=False)
        except AgentSandboxError as err:
            done.append(f"{old.name} ({old.template}) is out of date ({old.differs}) but was left: {err}")
            continue
        done.append(f"Replaced {old.name}, an idle sandbox of {old.template} out of date ({old.differs}): "
                    "the pool makes a new one from the template.")
    return done
