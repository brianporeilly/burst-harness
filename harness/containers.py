"""Local containers as disposable sandboxes: the podman/docker twin of agent_sandbox.

`--target podman` (or `docker`) with no `--docker-container` starts a fresh
container from `image` for the run, works in it, and removes it at the end --
the same lifecycle as claiming from a warm pool, so a laptop and the cluster
behave alike. With `--docker-container NAME` it attaches to an existing
container instead and leaves it alone afterwards.

The container gets no host mounts, no capabilities, and no credentials; the
harness clones into it and pushes out of it with a token passed per command.
"""

from __future__ import annotations

import json
import secrets
import shlex
import shutil
import subprocess

from .agent_sandbox import LABEL_MANAGED_BY, LABEL_TASK, MANAGED_BY, REQUIRED_COMMANDS, label_value
from .config import HarnessConfig
from .exec_target import DockerTarget


class ContainerError(RuntimeError):
    """The container could not be started or lacks required commands."""


def run_argv(cfg: HarnessConfig, *, binary: str, name: str, slug: str) -> list[str]:
    return [
        binary, "run", "--detach", "--name", name,
        "--label", f"{LABEL_MANAGED_BY}={MANAGED_BY}",
        "--label", f"{LABEL_TASK}={label_value(slug)}",
        # Nothing in the sandbox needs privileges: git, a shell, the agent's tools.
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        *shlex.split(cfg.container_args),
        cfg.image,
    ]


def acquire(cfg: HarnessConfig, *, binary: str, repo: str, slug: str) -> DockerTarget:
    """Attach to `cfg.docker_container`, or start a fresh one from `cfg.image`."""
    if cfg.docker_container:
        return DockerTarget(repo=repo, container=cfg.docker_container, binary=binary)
    if not cfg.image:
        raise ContainerError(f"target={binary} needs --docker-container or --image.")
    name = f"burst-{label_value(slug)[:40]}-{secrets.token_hex(3)}"
    try:
        proc = subprocess.run(run_argv(cfg, binary=binary, name=name, slug=slug), capture_output=True, text=True)
    except FileNotFoundError as err:
        raise ContainerError(f"{binary} is not installed or not on PATH.") from err
    if proc.returncode != 0:
        raise ContainerError(f"{binary} run {cfg.image} failed: {proc.stderr.strip()}")
    print(f"Started {name} from {cfg.image}.")
    target = DockerTarget(repo=repo, container=name, binary=binary, owned=True)
    try:
        if missing := target.missing_commands(REQUIRED_COMMANDS):
            raise ContainerError(
                f"{cfg.image} is missing required commands: {', '.join(missing)}. "
                "See images/sandbox/Containerfile for the contract."
            )
    except Exception:
        target.release()
        raise
    return target


def list_containers() -> list[dict[str, str]]:
    """Containers the harness started, from podman and docker where installed."""
    found = []
    for binary in ("podman", "docker"):
        if not shutil.which(binary):
            continue
        proc = subprocess.run(
            [binary, "ps", "-a", "--filter", f"label={LABEL_MANAGED_BY}={MANAGED_BY}", "--format", "json"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        text = proc.stdout.strip()
        # podman prints one JSON array; docker prints one object per line.
        items = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines()]
        for item in items:
            labels = item.get("Labels") or {}
            if isinstance(labels, str):  # docker: "k=v,k=v"
                labels = dict(pair.split("=", 1) for pair in labels.split(",") if "=" in pair)
            names = item.get("Names")
            found.append({
                "binary": binary,
                "name": names[0] if isinstance(names, list) else str(names),
                "task": labels.get(LABEL_TASK, "?"),
                "status": str(item.get("State") or item.get("Status") or "?"),
                "image": str(item.get("Image", "")),
            })
    return found
