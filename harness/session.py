"""`harness session`: a long-lived sandbox you talk to, rather than a task that runs.

A session is a container behind a gateway, a workspace that outlives it, and an
agent you reach from the browser: opencode's web UI (`--agent opencode`, the
default), or Claude Code running in a terminal in the browser (`--agent
claude-code`). There is no branch, no task document and no terminal state: you
(and the agent) decide what happens in it. A repo is optional -- without
`--repo-url` the workspace starts empty.

    harness session start NAME     start it, or say where a running one is
    harness session list           sessions, running and stopped
    harness session stop NAME      remove the containers; the workspace stays
    harness session rm NAME        stop, and delete the workspace too

`start --terminal` adds a terminal in the browser (zellij), on its own port and
behind its own login token, reached the same way. It is a shell in the sandbox:
whoever can log in to it can do what the agent can. A Claude Code session is
that terminal, opened on a zellij session running `claude`.

`start --allow a.com,llm.lan:8080` sets the session's own allowlist entries,
kept with the session and added to the gateway's allowlist each time it starts
(as is anything approved for it with `harness egress approve --scope
permanent`).

`start --idle 2h` stops a session that has had nothing go in or out for that
long, and `--lifetime` stops it regardless. No process of the harness's outlives
`start`, so it is the gateway that keeps count and exits; the sandbox watches
for that and exits too. A session stopped this way is as if you had run `stop`:
`start` brings it back.

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
# zellij's web server listens on loopback only (anything else needs a
# certificate); socat on TERMINAL_PORT is what the gateway passes connections to.
ZELLIJ_PORT = 8082
TERMINAL_PORT = 8083
ZELLIJ_CONFIG = "web_server true\nshow_startup_tips false\nshow_release_notes false\n"
AGENTS = ("opencode", "claude-code")
# The zellij session a Claude Code session's terminal opens on, and its layout:
# `claude` in the workspace, falling back to a shell when it exits.
CLAUDE_ZELLIJ_SESSION = "claude"
CLAUDE_LAYOUT = """layout {
    pane size=1 borderless=true { plugin location="tab-bar"; }
    pane command="bash" { args "-lc" "claude; exec bash"; }
    pane size=2 borderless=true { plugin location="status-bar"; }
}
"""
# What Claude Code reads from its environment, passed from the driver's (or as
# placeholders, with `--egress-inject model`).
CLAUDE_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
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


DURATION = re.compile(r"(\d+)([smhd]?)")
UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def seconds(text: str) -> int:
    """90m, 2h, 1d, or a number of seconds."""
    if not (match := DURATION.fullmatch(text.strip().lower())):
        raise SessionError(f"Not a duration: {text!r}. Use 90m, 2h, 1d, or seconds.")
    return int(match[1]) * UNITS[match[2]]


def model_env(cfg: HarnessConfig) -> dict[str, str]:
    """Model credentials opencode gets in its environment: the driver's Anthropic
    key, or its placeholder when the gateway is the one that holds the key."""
    if cfg.provider != "anthropic" or not os.environ.get("ANTHROPIC_API_KEY"):
        return {}
    if "model" in cfg.egress_inject:
        return {"ANTHROPIC_API_KEY": containers.MODEL_CREDENTIALS["ANTHROPIC_API_KEY"][1]}
    return {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}


def opencode_config(cfg: HarnessConfig) -> dict[str, Any] | None:
    """opencode's config for the harness's model settings: Anthropic with a key on
    the driver, or an OpenAI-compatible endpoint (llama.cpp's server is one).
    None for anything else."""
    if model_env(cfg):
        # opencode knows Anthropic already; it only needs telling which model.
        return {"$schema": "https://opencode.ai/config.json", "model": f"anthropic/{cfg.model_id}",
                "autoupdate": False, "share": "disabled"}
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
    if not config or "provider" not in config:
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
    """True once something answers HTTP there (a 401 counts: it is asking for the password)."""
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


def urls(record: dict[str, Any]) -> dict[str, str]:
    """Where the session's UI and terminal are, from this machine."""
    host = _local(record["bind"])
    found = {}
    if terminal := record.get("terminal"):
        found["terminal"] = f"http://{host}:{terminal['port']}/" + (terminal.get("path") or "")
    found["ui"] = found["terminal"] if record.get("agent") == "claude-code" else f"http://{host}:{record['port']}/"
    return found


def describe(record: dict[str, Any]) -> str:
    where = urls(record)["ui"]
    if record["bind"] == "0.0.0.0":
        where += f"  (and on this machine's other addresses, port {record['port']})"
    if record.get("agent") == "claude-code":
        lines = [f"  UI:        {where}  (Claude Code, in a terminal)", f"  token:     {record['terminal']['token']}"]
    else:
        lines = [f"  UI:        {where}", f"  user:      {UI_USER}", f"  password:  {record['password']}"]
    if record.get("allow"):
        lines.append(f"  allows:    {', '.join(record['allow'])} (as well as the defaults)")
    limits = [f"{what} {record[key]}s" for what, key in (("idle", "idle"), ("lifetime", "lifetime")) if record.get(key)]
    if limits:
        lines.append(f"  stops:     after {', '.join(limits)}")
    if (terminal := record.get("terminal")) and record.get("agent") != "claude-code":
        lines += [f"  terminal:  {urls(record)['terminal']}", f"  token:     {terminal['token']}"]
    return "\n".join(lines)


def claude_env(cfg: HarnessConfig) -> dict[str, str]:
    """What a Claude Code session's terminal gets in its environment."""
    found = {name: os.environ[name] for name in CLAUDE_ENV if os.environ.get(name)}
    if "model" in cfg.egress_inject:
        found = {name: containers.MODEL_CREDENTIALS[name][1] if name in containers.MODEL_CREDENTIALS else value
                 for name, value in found.items()}
    return found


def start_terminal(box: str, workdir: str, *, env: dict[str, str] | None = None, claude: bool = False) -> str:
    """Start zellij's web server and the relay in front of it. Returns a new login token.

    `env` is passed, by name, to zellij and so to every shell it opens. With
    `claude`, a zellij session running Claude Code is started for the browser
    to attach to.
    """
    env = env or {}

    def run(*argv: str, detach: bool = False) -> subprocess.CompletedProcess[str]:
        names = [arg for key in env for arg in ("-e", key)]
        return _podman("exec", *(["--detach"] if detach else []), "--workdir", workdir, *names, box, *argv, env=env)

    # zellij will not start its web server without a config file. One you have
    # edited (it is in the home volume) is left alone.
    setup = ('mkdir -p "$HOME/.config/zellij" && { [ -f "$HOME/.config/zellij/config.kdl" ] || '
             f'printf {shlex.quote(ZELLIJ_CONFIG)} > "$HOME/.config/zellij/config.kdl"; }}')
    if claude:
        layout = '"$HOME/.config/zellij/layouts/harness-claude.kdl"'
        setup += f' && mkdir -p "$HOME/.config/zellij/layouts" && printf %s {shlex.quote(CLAUDE_LAYOUT)} > {layout}'
    if (proc := run("sh", "-c", setup)).returncode != 0:
        raise SessionError(f"Could not write zellij's config: {proc.stderr.strip()}")
    for command in (f"exec zellij web --start --port {ZELLIJ_PORT} >> /tmp/zellij-web.log 2>&1",
                    f"exec socat TCP-LISTEN:{TERMINAL_PORT},fork,reuseaddr TCP:127.0.0.1:{ZELLIJ_PORT}"):
        if (proc := run("sh", "-c", command, detach=True)).returncode != 0:
            raise SessionError(f"Could not start the terminal: {proc.stderr.strip()}")
    if claude:
        # A fresh one each start: zellij would otherwise offer to resurrect the
        # last container's, with `claude` waiting for a keypress.
        run("zellij", "delete-session", "--force", CLAUDE_ZELLIJ_SESSION)
        proc = run("zellij", "--layout", "harness-claude", "attach", "--create-background", CLAUDE_ZELLIJ_SESSION)
        if proc.returncode != 0:
            raise SessionError(f"Could not start Claude Code's terminal session: {(proc.stderr or proc.stdout).strip()}")
    # Tokens are shown once and stored hashed, so each start makes a fresh one
    # and retires the rest: the one on record is always the one that works.
    run("zellij", "web", "--revoke-all-tokens")
    proc = run("zellij", "web", "--create-token")
    match = re.search(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", proc.stdout)
    if proc.returncode != 0 or not match:
        raise SessionError(f"Could not create a login token for the terminal: {(proc.stderr or proc.stdout).strip()}")
    return match[0]


def start(cfg: HarnessConfig, name: str, **options: Any) -> str:
    """Start the session, or report a running one. Returns what to tell the user. See launch()."""
    record, notes, already = launch(cfg, name, **options)
    if already:
        return f"Session {name} is already running.\n{describe(record)}"
    box = container_name(name)
    return "\n".join([f"Session {name} is up ({box}, egress {record['egress']} via {box}-egress).", *notes, describe(record)])


def launch(
    cfg: HarnessConfig, name: str, *, port: int = 0, bind: str = "", config_file: str = "",
    terminal: bool | None = None, terminal_port: int = 0, idle: str | None = None, lifetime: str | None = None,
    agent: str | None = None, allow: tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], list[str], bool]:
    """Start the session, or find it running. Returns (its record, notes for the
    user, whether it was already running).

    `terminal`, `idle`, `lifetime`, `agent` and `allow` None keep whatever the
    session had last time. `allow` replaces the session's own allowlist entries.
    """
    box = container_name(name)
    if not shutil.which(BINARY):
        raise SessionError(f"{BINARY} is not installed or not on PATH.")
    record = load_record(cfg, name)
    if state(name) == "running" and record:
        return record, [], True
    agent = agent or record.get("agent") or "opencode"
    if agent not in AGENTS:
        raise SessionError(f"A session's agent is one of {', '.join(AGENTS)} (got {agent!r}).")
    claude = agent == "claude-code"
    allowed = list(dict.fromkeys(allow if allow is not None else record.get("allow") or []))
    # Whatever is left of an earlier run goes; the volumes are what carry over.
    _target(cfg, name, WORKSPACE).release()

    if claude:
        config, endpoint = None, ()
    elif config_file:
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

    # A session is reached through its gateway, so it always has one. Its model
    # calls come from the container, which is what `--egress-inject model` is for.
    cfg = replace(cfg, egress="log" if cfg.egress == "open" else cfg.egress, engine="opencode")
    if record and bool(record.get("root")) != cfg.container_root:
        raise SessionError(
            f"Session {name} was created {'with' if record.get('root') else 'without'} --root, and its files are "
            "owned accordingly. Start it the same way, or `session rm` it and start afresh."
        )
    try:
        containers.check_root(cfg, BINARY)
    except ContainerError as err:
        raise SessionError(str(err)) from err
    limits = {key: seconds(given) if given is not None else int(record.get(key) or 0)
              for key, given in (("idle", idle), ("lifetime", lifetime))}
    gateway_env = tuple(f"EGRESS_{key.upper()}={value}" for key, value in limits.items() if value)
    bind = bind or record.get("bind") or "127.0.0.1"
    port = port or record.get("port") or _free_port(bind)
    password = record.get("password") or secrets.token_urlsafe(18)
    had_terminal = record.get("terminal") or {}
    if terminal is None:
        terminal = bool(had_terminal)
    if claude:
        # Claude Code's UI is the terminal, so the session's port is the terminal's.
        terminal, terminal_port = True, port
        inbound = [(bind, port, TERMINAL_PORT)]
    else:
        inbound = [(bind, port, UI_PORT)]
        if terminal:
            terminal_port = terminal_port or (had_terminal.get("port") if had_terminal.get("port") != port else 0) \
                or _free_port(bind)
            inbound.append((bind, terminal_port, TERMINAL_PORT))
    repo = f"{WORKSPACE}/repo" if cfg.repo_url else WORKSPACE
    label = ("--label", f"{containers.LABEL_SESSION}={name}")
    slug = f"session-{name}"

    try:
        network, proxy, flags = containers.start_egress(
            cfg, binary=BINARY, name=box, slug=slug, allow=(*endpoint, *allowed), inbound=tuple(inbound), labels=label,
            gateway_env=gateway_env,
        )
    except ContainerError as err:
        raise SessionError(str(err)) from err
    target = DockerTarget(repo=repo, container=box, binary=BINARY, owned=True, proxy=proxy, network=network)
    try:
        mounts = [arg for volume, path in volumes(name).items() for arg in ("--volume", f"{volume}:{path}")]
        # Up for as long as the gateway is: when that exits (idle, lifetime, or a crash), so does this.
        wait = ["burst-sandbox-wait", containers.gateway_address(flags), str(containers.PROXY_PORT)]
        proc = _podman(*containers.run_argv(cfg, binary=BINARY, name=box, slug=slug,
                                             network_args=[*flags, *label, *mounts])[1:], *wait)
        if proc.returncode != 0:
            raise SessionError(f"{BINARY} run {cfg.image} failed: {proc.stderr.strip()}")
        needed = (*REQUIRED_COMMANDS, "claude" if claude else "opencode", *(("zellij", "socat") if terminal else ()))
        if missing := target.missing_commands(needed):
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
        if claude:
            pass  # Claude Code needs no config written: burst-sandbox-init composes its own at start.
        elif config is not None:
            target.run_shell(f"mkdir -p {shlex.quote(str(Path(OPENCODE_CONFIG).parent))} && cat > {shlex.quote(OPENCODE_CONFIG)}",
                             stdin=json.dumps(config, indent=2) + "\n", cwd="/")
        else:
            notes.append(
                f"No model configured for opencode: provider={cfg.provider} is not one the harness can describe to "
                "it. Set one up in the UI, or pass --opencode-config FILE."
            )

        # The password, and any model key, go in by name from this process's
        # environment: they are in neither a command line nor the container's
        # own configuration.
        if not claude:
            serve = f"exec opencode web --hostname 0.0.0.0 --port {UI_PORT} >> /tmp/opencode.log 2>&1"
            secret_env = {"OPENCODE_SERVER_PASSWORD": password, **model_env(cfg)}
            proc = _podman("exec", "--detach", "--workdir", repo, *(arg for key in secret_env for arg in ("-e", key)),
                           box, "sh", "-c", serve, env=secret_env)
            if proc.returncode != 0:
                raise SessionError(f"Could not start opencode: {proc.stderr.strip()}")
            if not _wait_for_ui(bind, port):
                log = target.run_shell("tail -n 20 /tmp/opencode.log", check=False, cwd="/").stdout.strip()
                raise SessionError(f"opencode's UI did not come up on port {port}.\n{log}")
        elif not claude_env(cfg):
            notes.append(f"No Claude Code credential on the driver ({', '.join(CLAUDE_ENV[:2])}): log in from "
                         "the terminal, or start the session again with one set.")
        token = ""
        if terminal:
            token = start_terminal(box, repo, env=claude_env(cfg) if claude else None, claude=claude)
            if not _wait_for_ui(bind, terminal_port):
                log = target.run_shell("tail -n 20 /tmp/zellij-web.log", check=False, cwd="/").stdout.strip()
                raise SessionError(f"The terminal did not come up on port {terminal_port}.\n{log}")
    except BaseException:
        target.release()
        raise

    record = {
        "name": name, "bind": bind, "port": port, "password": password,
        "repo_url": git.redact_url(cfg.repo_url), "image": cfg.image, "egress": cfg.egress,
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root": cfg.container_root, **limits, "agent": agent, "allow": allowed,
    }
    if terminal:
        record["terminal"] = {"port": terminal_port, "token": token,
                              **({"path": CLAUDE_ZELLIJ_SESSION} if claude else {})}
    save_record(cfg, name, record)
    return record, notes, False


def add_allow(cfg: HarnessConfig, name: str, entry: str) -> bool:
    """Add an entry to a session's own allowlist, for its next start. False if it had it already."""
    record = load_record(cfg, name)
    if not record:
        raise SessionError(f"No session named {name}.")
    if entry in record.get("allow", []):
        return False
    save_record(cfg, name, {**record, "allow": [*record.get("allow", []), entry]})
    return True


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
            "agent": record.get("agent") or "opencode",
            "ui": urls(record)["ui"] if running and record else "-",
            "repo": record.get("repo_url") or "-",
        })
    return rows
