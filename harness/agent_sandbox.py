"""kubernetes-sigs/agent-sandbox: claim a sandbox, work in it, release it.

The harness owns the whole lifecycle, so the sandbox can be dumb, disposable, and
hold no secrets:

1. Create a SandboxClaim against a SandboxWarmPool, labelled with the task and
   given a hard deadline (``lifecycle.shutdownTime`` + ``shutdownPolicy: Delete``)
   so a claim whose driver died is reaped by the controller rather than lingering
   with its PVC.
2. Wait for it to be Ready and resolve its pod (the Sandbox's name).
3. Drive the pod with ``kubectl exec`` -- this is a :class:`KubectlTarget`.
4. Delete the claim when the task reaches a terminal state.

Exec rather than the sandbox router/``sandboxd`` runtime API: exec works under
runc and gVisor alike, needs no in-sandbox daemon, and adds no client SDK. Only
``kubectl`` is required on the driver; in a pod it picks up the in-cluster
ServiceAccount on its own.

Nothing is passed into the claim that would force a cold start (``env``,
``additionalPodMetadata``), so claims are served from the warm pool. The task
labels live on the SandboxClaim itself, which is what ``harness list`` reads.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import HarnessConfig
from .exec_target import KubectlTarget
from .gateway_control import Gateway, GatewayError, registration, save_events
from .git import redact_url

API_VERSION = "extensions.agents.x-k8s.io/v1beta1"

LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
MANAGED_BY = "burst-harness"
LABEL_TASK = "burst-harness/task"
ANNOTATION_REPO = "burst-harness/repo"
ANNOTATION_BRANCH = "burst-harness/branch"

# What the harness and Strands' shell-backed file tools run inside the sandbox.
# A sandbox image must provide these; anything else is the task's business.
REQUIRED_COMMANDS = ("sh", "git", "base64", "mkdir", "dirname", "ls", "rm", "cat", "env")


class AgentSandboxError(RuntimeError):
    """A claim could not be created, became unready, or is missing its tools."""


def label_value(text: str) -> str:
    """Coerce text into a valid label value (<=63, alphanumeric at both ends)."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text)[:63]
    return cleaned.strip("-_.") or "task"


def claim_manifest(
    *, pool: str, slug: str, repo_url: str, branch: str, ttl_seconds: int, now: datetime | None = None
) -> dict[str, Any]:
    deadline = (now or datetime.now(timezone.utc)) + timedelta(seconds=ttl_seconds)
    return {
        "apiVersion": API_VERSION,
        "kind": "SandboxClaim",
        "metadata": {
            "generateName": f"{label_value(pool)[:40]}-",
            "labels": {LABEL_MANAGED_BY: MANAGED_BY, LABEL_TASK: label_value(slug)},
            "annotations": {ANNOTATION_REPO: redact_url(repo_url), ANNOTATION_BRANCH: branch},
        },
        "spec": {
            "warmPoolRef": {"name": pool},
            "lifecycle": {
                "shutdownTime": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "shutdownPolicy": "Delete",
            },
        },
    }


def _kubectl(cfg: HarnessConfig, *args: str, stdin: str | None = None, namespaced: bool = True) -> str:
    argv = [cfg.kubectl]
    if cfg.kube_context:
        argv += ["--context", cfg.kube_context]
    if namespaced and cfg.namespace:
        argv += ["-n", cfg.namespace]
    try:
        proc = subprocess.run([*argv, *args], capture_output=True, text=True, input=stdin, timeout=600)
    except FileNotFoundError as err:
        raise AgentSandboxError(f"{cfg.kubectl} is not installed or not on PATH.") from err
    if proc.returncode != 0:
        raise AgentSandboxError(f"kubectl {' '.join(args[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def create_claim(cfg: HarnessConfig, *, slug: str, repo_url: str, branch: str) -> str:
    manifest = claim_manifest(
        pool=cfg.pool, slug=slug, repo_url=repo_url, branch=branch, ttl_seconds=cfg.claim_ttl
    )
    return _kubectl(cfg, "create", "-f", "-", "-o", "jsonpath={.metadata.name}", stdin=json.dumps(manifest))


def wait_ready(cfg: HarnessConfig, claim: str, *, timeout: int = 300) -> str:
    """Block until the claim is Ready; return its sandbox's pod name."""
    _kubectl(cfg, "wait", f"sandboxclaim/{claim}", "--for=condition=Ready", f"--timeout={timeout}s")
    pod = _kubectl(cfg, "get", "sandboxclaim", claim, "-o", "jsonpath={.status.sandbox.name}")
    if not pod:
        raise AgentSandboxError(f"SandboxClaim {claim} is Ready but names no sandbox.")
    return pod


def pod_address(cfg: HarnessConfig, pod: str) -> str:
    """The pod's IP, which is how the gateway knows it."""
    address = _kubectl(cfg, "get", "pod", pod, "-o", "jsonpath={.status.podIP}")
    if not address:
        raise AgentSandboxError(f"Pod {pod} has no IP address yet.")
    return address


def seconds_left(cfg: HarnessConfig, claim: str, now: datetime | None = None) -> int:
    """How long until the claim's deadline (its lifecycle.shutdownTime), at least a minute."""
    raw = _kubectl(cfg, "get", "sandboxclaim", claim, "-o", "jsonpath={.spec.lifecycle.shutdownTime}")
    if not raw:
        return cfg.claim_ttl
    deadline = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return max(60, int((deadline - (now or datetime.now(timezone.utc))).total_seconds()))


def delete_claim(cfg: HarnessConfig, claim: str) -> None:
    _kubectl(cfg, "delete", "sandboxclaim", claim, "--wait=false", "--ignore-not-found")


def list_claims(cfg: HarnessConfig) -> list[dict[str, Any]]:
    raw = _kubectl(cfg, "get", "sandboxclaims", "-l", f"{LABEL_MANAGED_BY}={MANAGED_BY}", "-o", "json")
    return json.loads(raw).get("items", [])


def list_jobs(cfg: HarnessConfig) -> list[dict[str, Any]]:
    """Harness Jobs in any namespace, by their task label. Empty if not permitted."""
    try:
        raw = _kubectl(cfg, "get", "jobs", "-A", "-l", LABEL_TASK, "-o", "json", namespaced=False)
    except AgentSandboxError:
        return []
    return json.loads(raw).get("items", [])


@dataclass(frozen=True)
class AgentSandboxTarget(KubectlTarget):
    """A claimed sandbox, driven over kubectl exec."""

    claim: str = ""
    # True when this run created the claim, and so may delete it.
    owned: bool = False
    # The shared gateway it is registered with, and where its events are saved on release.
    gateway: Gateway | None = None
    events: str = ""

    def describe(self) -> str:
        egress = f" (egress via {self.gateway.url})" if self.gateway else ""
        return f"agent-sandbox {self.claim} (pod {self.pod}) at {self.repo}{egress}"

    def release(self) -> str:
        if not self.owned:
            return ""
        saved = ""
        if self.gateway is not None:
            try:
                events = self.gateway.unregister(self.claim)
                saved = save_events(self.events, events) if self.events else ""
            except GatewayError as err:
                # Its registration lapses with the claim's deadline in any case.
                saved = f" Could not unregister it from the gateway ({err}); it lapses at the claim's deadline."
        subprocess.run(
            [*self.kubectl_base(), "delete", "sandboxclaim", self.claim, "--wait=false", "--ignore-not-found"],
            capture_output=True,
        )
        return f"Released {self.claim}.{saved}"

    def attach_hint(self) -> str:
        return f"--target agent-sandbox --claim {self.claim}"


def acquire(
    cfg: HarnessConfig, *, repo: str, slug: str, branch: str,
    allow: tuple[str, ...] = (), inject: tuple[dict[str, object], ...] = (),
    read_only: tuple[dict[str, object], ...] | None = None, upstream_ca: tuple[dict[str, str], ...] = (),
) -> AgentSandboxTarget:
    """Attach to `cfg.claim_name`, or claim a fresh sandbox from `cfg.pool`.

    With a shared gateway (`gateway_control`), the sandbox is registered with
    it before anything runs there: `allow`, `inject`, `read_only` and
    `upstream_ca` are as for a podman gateway (containers.start_egress).
    """
    gateway = Gateway.from_config(cfg)
    if gateway is None and (cfg.egress_inject or inject or read_only is not None or upstream_ca):
        raise AgentSandboxError("Adding credentials, or holding a sandbox to reads, needs the cluster's gateway: "
                                "set gateway_control.")
    # Worked out before claiming, so a setting that cannot be used claims nothing.
    body, summary = registration(cfg, address="", ttl=0, allow=allow, inject=inject, read_only=read_only,
                                 upstream_ca=upstream_ca) if gateway is not None else ({}, "")
    owned = not cfg.claim_name
    if owned:
        if not cfg.pool:
            raise AgentSandboxError("target=agent-sandbox needs --pool or --claim.")
        claim = create_claim(cfg, slug=slug, repo_url=cfg.repo_url, branch=branch)
        print(f"Claimed {claim} from pool {cfg.pool} (deleted by {cfg.claim_ttl}s from now at the latest).")
    else:
        claim = cfg.claim_name
    target: AgentSandboxTarget | None = None
    try:
        pod = wait_ready(cfg, claim)
        target = AgentSandboxTarget(
            repo=repo,
            pod=pod,
            namespace=cfg.namespace or None,
            container=cfg.container or None,
            context=cfg.kube_context or None,
            kubectl=cfg.kubectl,
            claim=claim,
            owned=owned,
        )
        if gateway is not None:
            from .containers import events_path  # containers imports this module

            # An attached claim is registered again too: the gateway may have
            # restarted since, and forgotten it. Its registration ends with the claim.
            body.update(address=pod_address(cfg, pod), ttl=cfg.claim_ttl if owned else seconds_left(cfg, claim))
            gateway.register(claim, body)
            target = replace(target, gateway=gateway, events=events_path(cfg, claim))
            print(f"Egress via {gateway.url} ({summary}).")
            # For the orchestrator: which sandbox the gateway knows this burst by, to show and approve what it
            # refuses while the burst runs, and where its events are kept once it ends.
            print("HARNESS_SANDBOX " + json.dumps({"claim": claim, "mode": body.get("mode", ""),
                                                   "events": target.events}), flush=True)
        if missing := target.missing_commands(REQUIRED_COMMANDS):
            raise AgentSandboxError(
                f"Sandbox image is missing required commands: {', '.join(missing)}. "
                "See images/sandbox/Containerfile for the contract."
            )
    except Exception:
        if owned:
            # Unregisters it as well, once it is registered.
            target.release() if target is not None else delete_claim(cfg, claim)
        raise
    return target
