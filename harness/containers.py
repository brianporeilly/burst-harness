"""Local containers as disposable sandboxes: the podman/docker twin of agent_sandbox.

`--target podman` (or `docker`) with no `--docker-container` starts a fresh
container from `image` for the run, works in it, and removes it at the end --
the same lifecycle as claiming from a warm pool, so a laptop and the cluster
behave alike. With `--docker-container NAME` it attaches to an existing
container instead and leaves it alone afterwards.

The container gets no host mounts, no capabilities, and no credentials; the
harness clones into it and pushes out of it with a token passed per command.

Optional gateway (`--egress log` or `allowlist`): the container is attached
only to its own `--internal` network (no route out), and a gateway sidecar --
`gateway_image`, see gateway/ -- sits on that network and the normal one. The
container's proxy variables and its resolver point at the sidecar by IP. `log`
lets everything through; `allowlist` allows CONNECT to listed hosts on port
443. Either way every decision is an event on the sidecar's stdout, so `podman
logs <name>-egress` is the audit trail, saved under `state_dir`/egress when the
sidecar and network are removed with the container.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .agent_sandbox import LABEL_MANAGED_BY, LABEL_TASK, MANAGED_BY, REQUIRED_COMMANDS, label_value
from .config import HarnessConfig
from .exec_target import DockerTarget


class ContainerError(RuntimeError):
    """The container could not be started or lacks required commands."""


PROXY_PORT = 3128
EGRESS_MODES = ("open", "log", "allowlist")
# The runtime's default network, which has a route out.
DEFAULT_NETWORK = {"podman": "podman", "docker": "bridge"}


def _labels(slug: str) -> list[str]:
    return ["--label", f"{LABEL_MANAGED_BY}={MANAGED_BY}", "--label", f"{LABEL_TASK}={label_value(slug)}"]


def run_argv(cfg: HarnessConfig, *, binary: str, name: str, slug: str, network_args: list[str] | tuple[str, ...] = ()) -> list[str]:
    return [
        binary, "run", "--detach", "--name", name, *_labels(slug),
        # Nothing in the sandbox needs privileges: git, a shell, the agent's tools.
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        *network_args,
        *shlex.split(cfg.container_args),
        cfg.image,
    ]


def allowlist(cfg: HarnessConfig) -> list[str]:
    """egress_allow plus the hosts this run needs to reach no matter what."""
    hosts = list(cfg.egress_allow)
    for url in (cfg.repo_url, os.environ.get("ANTHROPIC_BASE_URL", "")):
        parts = urlsplit(url)
        if parts.scheme == "https" and parts.hostname and parts.hostname not in hosts:
            hosts.append(parts.hostname)
        elif url and parts.scheme != "https" and url == cfg.repo_url:
            raise ContainerError(
                f"--egress allowlist only passes HTTPS; {url} is not an https:// URL."
            )
    return hosts


def check_reachable(cfg: HarnessConfig) -> None:
    """egress=log passes any host, but only through the proxy: git over ssh has no way out."""
    if cfg.repo_url and urlsplit(cfg.repo_url).scheme not in ("http", "https"):
        raise ContainerError(f"--egress {cfg.egress} only passes HTTP(S); {cfg.repo_url} is not an http(s):// URL.")


def events_path(cfg: HarnessConfig, name: str) -> str:
    """Where a container's gateway events are saved when it is removed."""
    return str(Path(cfg.state_dir).expanduser() / "egress" / f"{name}.jsonl")


def _run(binary: str, *args: str) -> str:
    proc = subprocess.run([binary, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise ContainerError(f"{binary} {' '.join(args[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def start_egress(cfg: HarnessConfig, *, binary: str, name: str, slug: str) -> tuple[str, str, list[str]]:
    """Create the internal network and gateway sidecar.

    Returns (network, proxy, run flags that put a container behind them).
    """
    if cfg.egress == "allowlist":
        hosts = allowlist(cfg)
        policy, summary = ["-e", f"EGRESS_ALLOW={','.join(hosts)}"], f"allowlist: {', '.join(hosts)}"
    else:
        check_reachable(cfg)
        policy, summary = [], "everything passes and is recorded"
    network, proxy = f"{name}-net", f"{name}-egress"
    create = ["network", "create", "--internal", network]
    if binary == "podman":
        # The network's own resolver is off: the sandbox reaches the gateway by
        # IP, and its only resolver is the gateway's DNS sink.
        create.insert(3, "--disable-dns")
    _run(binary, *create)
    try:
        _run(
            binary, "run", "--detach", "--name", proxy, *_labels(slug),
            "--network", DEFAULT_NETWORK.get(binary, "bridge"),
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
            # Lets the unprivileged gateway bind its DNS sink to port 53.
            "--sysctl", "net.ipv4.ip_unprivileged_port_start=0",
            "-e", f"EGRESS_MODE={cfg.egress}", "-e", f"EGRESS_LISTEN={PROXY_PORT}",
            "-e", f"EGRESS_SANDBOX={name}", *policy,
            cfg.gateway_image,
        )
        _run(binary, "network", "connect", network, proxy)
        ip = _run(binary, "inspect", proxy, "--format",
                  "{{(index .NetworkSettings.Networks \"%s\").IPAddress}}" % network)
    except ContainerError:
        subprocess.run([binary, "rm", "-f", "-t", "0", proxy], capture_output=True)
        subprocess.run([binary, "network", "rm", network], capture_output=True)
        raise
    url = f"http://{ip}:{PROXY_PORT}"
    env = [f"{k}={url}" for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")]
    env += ["NO_PROXY=localhost,127.0.0.1", "no_proxy=localhost,127.0.0.1",
            # Node's own fetch ignores the proxy variables unless told otherwise.
            "NODE_USE_ENV_PROXY=1"]
    if cfg.egress == "allowlist":
        # Claude Code's telemetry and update checks would only be refused.
        env.append("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1")
    # "." clears the host's search domains, which would otherwise be inherited.
    flags = ["--network", network, "--dns", ip, "--dns-search", "."]
    for item in env:
        flags += ["-e", item]
    print(f"Egress via {proxy} ({summary}).")
    return network, proxy, flags


def acquire(cfg: HarnessConfig, *, binary: str, repo: str, slug: str) -> DockerTarget:
    """Attach to `cfg.docker_container`, or start a fresh one from `cfg.image`."""
    if cfg.docker_container:
        return DockerTarget(repo=repo, container=cfg.docker_container, binary=binary)
    if not cfg.image:
        raise ContainerError(f"target={binary} needs --docker-container or --image.")
    if cfg.egress not in EGRESS_MODES:
        raise ContainerError(f"egress must be one of {', '.join(EGRESS_MODES)} (got {cfg.egress!r}).")
    if not shutil.which(binary):
        raise ContainerError(f"{binary} is not installed or not on PATH.")
    name = f"burst-{label_value(slug)[:40]}-{secrets.token_hex(3)}"
    network = proxy = ""
    network_args: list[str] = []
    if cfg.egress != "open":
        network, proxy, network_args = start_egress(cfg, binary=binary, name=name, slug=slug)
    proc = subprocess.run(
        run_argv(cfg, binary=binary, name=name, slug=slug, network_args=network_args),
        capture_output=True, text=True,
    )
    target = DockerTarget(repo=repo, container=name, binary=binary, owned=True, proxy=proxy, network=network,
                          events=events_path(cfg, name) if proxy else "")
    if proc.returncode != 0:
        target.release()
        raise ContainerError(f"{binary} run {cfg.image} failed: {proc.stderr.strip()}")
    print(f"Started {name} from {cfg.image}.")
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


def kept(cfg: HarnessConfig, *, binary: str) -> DockerTarget:
    """A container an earlier run kept, with its egress sidecar and network if it has them."""
    name = cfg.docker_container
    proxy, network = f"{name}-egress", f"{name}-net"
    has_proxy = subprocess.run([binary, "container", "exists" if binary == "podman" else "inspect", proxy],
                               capture_output=True).returncode == 0
    has_network = subprocess.run([binary, "network", "inspect", network], capture_output=True).returncode == 0
    return DockerTarget(repo="/", container=name, binary=binary, owned=True,
                        proxy=proxy if has_proxy else "", network=network if has_network else "",
                        events=events_path(cfg, name) if has_proxy else "")


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
