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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import HarnessConfig
from .exec_target import KubectlTarget
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

    def describe(self) -> str:
        return f"agent-sandbox {self.claim} (pod {self.pod}) at {self.repo}"

    def release(self) -> str:
        if not self.owned:
            return ""
        subprocess.run(
            [*self.kubectl_base(), "delete", "sandboxclaim", self.claim, "--wait=false", "--ignore-not-found"],
            capture_output=True,
        )
        return f"Released {self.claim}."

    def attach_hint(self) -> str:
        return f"--target agent-sandbox --claim {self.claim}"


def acquire(cfg: HarnessConfig, *, repo: str, slug: str, branch: str) -> AgentSandboxTarget:
    """Attach to `cfg.claim_name`, or claim a fresh sandbox from `cfg.pool`."""
    owned = not cfg.claim_name
    if owned:
        if not cfg.pool:
            raise AgentSandboxError("target=agent-sandbox needs --pool or --claim.")
        claim = create_claim(cfg, slug=slug, repo_url=cfg.repo_url, branch=branch)
        print(f"Claimed {claim} from pool {cfg.pool} (deleted by {cfg.claim_ttl}s from now at the latest).")
    else:
        claim = cfg.claim_name
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
        if missing := target.missing_commands(REQUIRED_COMMANDS):
            raise AgentSandboxError(
                f"Sandbox image is missing required commands: {', '.join(missing)}. "
                "See images/sandbox/Containerfile for the contract."
            )
    except Exception:
        if owned:
            delete_claim(cfg, claim)
        raise
    return target
