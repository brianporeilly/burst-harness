"""`harness session`: a long-lived sandbox you talk to, rather than a task that runs.

A session is a container behind a gateway, a workspace that outlives it, and
opencode's web UI. There is no branch, no task document and no terminal state:
you (and the agent you chat with) decide what happens in it. A repo is
optional -- without `--repo-url` the workspace starts empty.

    harness session start NAME     start it, or say where a running one is
    harness session list           sessions, running and stopped
    harness session stop NAME      remove the containers; the workspace stays
    harness session rm NAME        stop, and delete the workspace too

What persists is two volumes -- the workspace (`/workspace`) and the agent's
home (opencode's conversations and settings) -- so a stopped or crashed session
starts again where it was. What does not is anything installed outside them.

The UI is never published from the sandbox itself, which has no way in or out
except its gateway: the gateway listens on the host and passes connections in,
recording each. opencode asks for a password, generated per session and kept in
`state_dir`/sessions (0600) so `start` can show it again.

podman only, for now: the gateway has to know the sandbox's address ahead of
time, which needs a network podman lets us lay out.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import containers, git
from .agent_sandbox import REQUIRED_COMMANDS
from .config import HarnessConfig
from .containers import ContainerError
from .exec_target import DockerTarget

BINARY = "podman"
UI_PORT = 4096
UI_USER = "opencode"
WORKSPACE = "/workspace"
HOME = "/home/agent"
OPENCODE_CONFIG = f"{HOME}/.config/opencode/opencode.json"


class SessionError(RuntimeError):
    """A session could not be started, found, or removed."""


def container_name(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
        raise SessionError("A session name is lowercase letters, digits and dashes, at most 40 characters.")
    return f"burst-session-{name}"


def volumes(name: str) -> dict[str, str]:
    """Volume name -> mount point. These are the session; the containers are replaceable."""
    base = container_name(name)
    return {f"{base}-work": WORKSPACE, f"{base}-home": HOME}


def record_path(cfg: HarnessConfig, name: str) -> Path:
    return Path(cfg.state_dir).expanduser() / "sessions" / f"{name}.json"


def load_record(cfg: HarnessConfig, name: str) -> dict[str, Any]:
    path = record_path(cfg, name)
    return json.loads(path.read_text()) if path.is_file() else {}


def save_record(cfg: HarnessConfig, name: str, record: dict[str, Any]) -> None:
    path = record_path(cfg, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps(record, indent=2) + "\n")


def opencode_config(cfg: HarnessConfig) -> dict[str, Any] | None:
    """opencode's config for the harness's model settings, where they describe an
    OpenAI-compatible endpoint (llama.cpp's server is one). None for anything else."""
    if cfg.provider == "llamacpp" and cfg.base_url:
        # As for the Strands engine: base_url is the server's root.
        base = cfg.base_url.rstrip("/") + "/v1"
    elif cfg.provider == "openai" and cfg.base_url:
        base = cfg.base_url
    else:
        return None
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"harness": {
            "npm": "@ai-sdk/openai-compatible",
            "name": cfg.provider,
            "options": {"baseURL": base, "apiKey": os.environ.get("OPENAI_API_KEY") or "none"},
            "models": {cfg.model_id: {
                "name": cfg.model_id,
                "limit": {"context": cfg.context_window_limit,
                          "output": min(cfg.max_tokens, cfg.context_window_limit // 2)},
            }},
        }},
        "model": f"harness/{cfg.model_id}",
        # The image is immutable and pinned; nothing is shared to opencode's service.
        "autoupdate": False,
        "share": "disabled",
    }


def model_endpoint(config: dict[str, Any] | None) -> tuple[str, ...]:
    """The allowlist entry that lets the session reach the model in `config`."""
    if not config:
        return ()
    url = urlsplit(config["provider"]["harness"]["options"]["baseURL"])
    if not url.hostname:
        return ()
    port = url.port or (443 if url.scheme == "https" else 80)
    return (url.hostname if port == 443 else f"{url.hostname}:{port}",)


def _podman(*args: str, env: dict[str, str] | None = None, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([BINARY, *args], capture_output=True, text=True, input=stdin,
                          env={**os.environ, **env} if env else None)


def state(name: str) -> str:
    """running, stopped (the container is there but not up), or absent."""
    proc = _podman("inspect", container_name(name), "--format", "{{.State.Running}}")
    if proc.returncode != 0:
        return "absent"
    return "running" if proc.stdout.strip() == "true" else "stopped"


def _target(cfg: HarnessConfig, name: str, repo: str) -> DockerTarget:
    box = container_name(name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return DockerTarget(repo=repo, container=box, binary=BINARY, owned=True, proxy=f"{box}-egress", network=f"{box}-net",
                        events=containers.events_path(cfg, f"{box}-{stamp}"))


def _free_port(bind: str) -> int:
    with socket.socket() as probe:
        probe.bind((bind, 0))
        return probe.getsockname()[1]


def _local(bind: str) -> str:
    """An address on this machine that reaches something bound to `bind`."""
    return "127.0.0.1" if bind == "0.0.0.0" else bind


def _wait_for_ui(bind: str, port: int, timeout: float = 60) -> bool:
    """True once the UI answers (a 401 counts: it is asking for the password)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://{_local(bind)}:{port}/", timeout=3)
            return True
        except urllib.error.HTTPError:
            return True
        except OSError:
            time.sleep(0.5)
    return False


def describe(record: dict[str, Any]) -> str:
    where = f"http://{_local(record['bind'])}:{record['port']}/"
    if record["bind"] == "0.0.0.0":
        where += f"  (and on this machine's other addresses, port {record['port']})"
    return f"  UI:        {where}\n  user:      {UI_USER}\n  password:  {record['password']}"


def start(cfg: HarnessConfig, name: str, *, port: int = 0, bind: str = "", config_file: str = "") -> str:
    """Start the session, or report a running one. Returns what to tell the user."""
    box = container_name(name)
    if not shutil.which(BINARY):
        raise SessionError(f"{BINARY} is not installed or not on PATH.")
    record = load_record(cfg, name)
    if state(name) == "running" and record:
        return f"Session {name} is already running.\n{describe(record)}"
    # Whatever is left of an earlier run goes; the volumes are what carry over.
    _target(cfg, name, WORKSPACE).release()

    if config_file:
        try:
            config = json.loads(Path(config_file).expanduser().read_text())
            if not isinstance(config, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as err:
            raise SessionError(f"{config_file}: {err}") from err
        endpoint: tuple[str, ...] = ()
    else:
        config = opencode_config(cfg)
        endpoint = model_endpoint(config)

    # A session is reached through its gateway, so it always has one.
    if cfg.egress == "open":
        cfg = replace(cfg, egress="log")
    bind = bind or record.get("bind") or "127.0.0.1"
    port = port or record.get("port") or _free_port(bind)
    password = record.get("password") or secrets.token_urlsafe(18)
    repo = f"{WORKSPACE}/repo" if cfg.repo_url else WORKSPACE
    label = ("--label", f"{containers.LABEL_SESSION}={name}")
    slug = f"session-{name}"

    try:
        network, proxy, flags = containers.start_egress(
            cfg, binary=BINARY, name=box, slug=slug, allow=endpoint, inbound=((bind, port, UI_PORT),), labels=label,
        )
    except ContainerError as err:
        raise SessionError(str(err)) from err
    target = DockerTarget(repo=repo, container=box, binary=BINARY, owned=True, proxy=proxy, network=network)
    try:
        mounts = [arg for volume, path in volumes(name).items() for arg in ("--volume", f"{volume}:{path}")]
        proc = _podman(*containers.run_argv(cfg, binary=BINARY, name=box, slug=slug,
                                             network_args=[*flags, *label, *mounts])[1:])
        if proc.returncode != 0:
            raise SessionError(f"{BINARY} run {cfg.image} failed: {proc.stderr.strip()}")
        if missing := target.missing_commands((*REQUIRED_COMMANDS, "opencode")):
            raise SessionError(f"{cfg.image} is missing: {', '.join(missing)}. See images/sandbox/Containerfile.")
        if cfg.egress_inject:
            containers.trust_gateway(BINARY, proxy=proxy, name=box)

        notes = []
        if cfg.repo_url:
            if git.is_repo(target):
                notes.append(f"Using the checkout already in {repo}.")
            else:
                auth = () if "git" in cfg.egress_inject else git.auth_args(cfg.repo_url)
                notes.append(git.clone(target, cfg.repo_url, auth=auth))
        if config is not None:
            target.run_shell(f"mkdir -p {shlex.quote(str(Path(OPENCODE_CONFIG).parent))} && cat > {shlex.quote(OPENCODE_CONFIG)}",
                             stdin=json.dumps(config, indent=2) + "\n", cwd="/")
        else:
            notes.append(
                f"No model configured for opencode: provider={cfg.provider} is not one the harness can describe to "
                "it. Set one up in the UI, or pass --opencode-config FILE."
            )

        # The password goes in by name, from this process's environment: it is in
        # neither a command line nor the container's own configuration.
        serve = f"exec opencode web --hostname 0.0.0.0 --port {UI_PORT} >> /tmp/opencode.log 2>&1"
        proc = _podman("exec", "--detach", "--workdir", repo, "-e", "OPENCODE_SERVER_PASSWORD", box, "sh", "-c", serve,
                       env={"OPENCODE_SERVER_PASSWORD": password})
        if proc.returncode != 0:
            raise SessionError(f"Could not start opencode: {proc.stderr.strip()}")
        if not _wait_for_ui(bind, port):
            log = target.run_shell("tail -n 20 /tmp/opencode.log", check=False, cwd="/").stdout.strip()
            raise SessionError(f"opencode's UI did not come up on port {port}.\n{log}")
    except BaseException:
        target.release()
        raise

    record = {
        "name": name, "bind": bind, "port": port, "password": password,
        "repo_url": git.redact_url(cfg.repo_url), "image": cfg.image, "egress": cfg.egress,
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_record(cfg, name, record)
    lines = [f"Session {name} is up ({box}, egress {cfg.egress} via {proxy}).", *notes, describe(record)]
    return "\n".join(lines)


def stop(cfg: HarnessConfig, name: str) -> str:
    """Remove the session's containers and network. Its volumes, and so its work, stay."""
    if state(name) == "absent" and not load_record(cfg, name):
        raise SessionError(f"No session named {name}.")
    done = _target(cfg, name, WORKSPACE).release()
    return f"Stopped session {name}; its workspace is kept. {done}"


def remove(cfg: HarnessConfig, name: str) -> str:
    """Stop the session and delete its volumes and record: everything in its workspace goes."""
    _target(cfg, name, WORKSPACE).release()
    gone = [v for v in volumes(name) if _podman("volume", "rm", "--force", v).returncode == 0]
    record_path(cfg, name).unlink(missing_ok=True)
    return f"Removed session {name} and its volumes ({', '.join(gone) or 'none found'})."


def list_sessions(cfg: HarnessConfig) -> list[dict[str, str]]:
    """Every session with a record or a volume, and whether it is running."""
    names = {path.stem for path in (Path(cfg.state_dir).expanduser() / "sessions").glob("*.json")}
    proc = _podman("volume", "ls", "--format", "{{.Name}}")
    for volume in proc.stdout.split() if proc.returncode == 0 else []:
        if match := re.fullmatch(r"burst-session-(.+)-work", volume):
            names.add(match[1])
    rows = []
    for name in sorted(names):
        record = load_record(cfg, name)
        running = state(name) == "running"
        rows.append({
            "name": name,
            "status": "running" if running else "stopped",
            "ui": f"http://{record['bind']}:{record['port']}/" if running and record else "-",
            "repo": record.get("repo_url") or "-",
        })
    return rows
