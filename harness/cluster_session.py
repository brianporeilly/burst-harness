"""`harness session` on a cluster (target=agent-sandbox): the same sessions as on
podman, in a sandbox claimed from a warm pool, behind the cluster's shared
gateway (ROADMAP phase 6, step 4).

    harness session start NAME     claim it (or resume it), register it, serve its UI
    harness session stop NAME      suspend it: the pod goes, the workspace stays
    harness session rm NAME        delete the claim, and the workspace with it
    harness session list           sessions on the cluster, running and suspended

A session is a SandboxClaim named `session-NAME`, with no deadline unless
`--lifetime` gives one. Stopping sets its Sandbox's operatingMode to Suspended,
which deletes the pod and keeps the workspace volume; starting sets it back to
Running, and the new pod mounts the same volume. A suspended pod takes a while
to go (its grace period), and the Sandbox still says Ready while it does, so
`stop` waits for it to be gone and `start` waits for a pod that is not going. The agent's home (opencode's
conversations, Claude Code's state) is kept there too, at /workspace/.home,
since the pool's template mounts nothing else.

The UI is reached the way it is on podman, through the gateway and on its
record: an HTTPRoute per host name (`session-NAME.<session_domain>`, and
`session-NAME-term.<session_domain>` for opencode's terminal) sends it to the
gateway's EGRESS_INBOUND_HTTP port, and the registration says which of the
sandbox's ports each name goes to. The routes belong to the claim, so deleting
the claim deletes them; a suspended session's names get a 404 from the gateway,
which no longer has them.

A pod's address changes each time it starts, so `start` registers the
session every time, running or not. A gateway with EGRESS_STATE keeps
registrations across its restarts; after a restart of one without, `start` is
what gives a running session its network back.

`--idle` goes in the registration. The gateway, which has no say over pods,
only reports how long each sandbox has done nothing; `harness session reap`,
run every few minutes by something holding the driver's rights (a CronJob),
suspends each session idle for longer, as `stop` would. `start` brings it back.

Not here yet: `--root` (the pool's template runs as uid 1000; user-namespace
pods work on this cluster, so a template for root sessions is possible), and
`--port`/`--bind`, which mean nothing on a cluster.
"""

from __future__ import annotations

import base64
import json
import secrets
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import git
from .agent_sandbox import (
    API_VERSION,
    LABEL_MANAGED_BY,
    MANAGED_BY,
    REQUIRED_COMMANDS,
    AgentSandboxError,
    _kubectl,
    label_value,
)
from .config import HarnessConfig
from .containers import ContainerError
from .exec_target import KubectlTarget
from .gateway_control import Gateway, GatewayError, registration, save_events
from .session import (
    AGENTS,
    OPENCODE_HOSTS,
    TERMINAL_PORT,
    UI_PORT,
    UI_USER,
    WORKSPACE,
    SessionError,
    claude_env,
    container_name,
    load_record,
    model_endpoint,
    model_env,
    open_in,
    opencode_config,
    record_path,
    save_record,
    seconds,
    terminal_in,
    terminal_session,
)

LABEL_SESSION = "burst-harness/session"
SANDBOX_API = "agents.x-k8s.io/v1beta1"
ROUTE_API = "gateway.networking.k8s.io/v1"
# The agent's home, on the workspace volume so it outlives the pod.
HOME = f"{WORKSPACE}/.home"
OPENCODE_CONFIG = f"{HOME}/.config/opencode/opencode.json"


def claim_name(name: str) -> str:
    container_name(name)  # the same rule for names as on podman
    return f"session-{name}"


def hosts(cfg: HarnessConfig, name: str, *, claude: bool, terminal: bool) -> dict[str, int]:
    """Each host name the session answers to, and the port in the sandbox it goes to."""
    base = f"session-{name}"
    if claude:
        return {f"{base}.{cfg.session_domain}": TERMINAL_PORT}
    found = {f"{base}.{cfg.session_domain}": UI_PORT}
    if terminal:
        found[f"{base}-term.{cfg.session_domain}"] = TERMINAL_PORT
    return found


def claim_manifest(cfg: HarnessConfig, name: str, *, lifetime: int, now: datetime | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"warmPoolRef": {"name": cfg.pool}}
    if lifetime:
        deadline = (now or datetime.now(timezone.utc)) + timedelta(seconds=lifetime)
        spec["lifecycle"] = {"shutdownTime": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"), "shutdownPolicy": "Delete"}
    return {
        "apiVersion": API_VERSION,
        "kind": "SandboxClaim",
        "metadata": {
            "name": claim_name(name),
            "labels": {LABEL_MANAGED_BY: MANAGED_BY, LABEL_SESSION: label_value(name)},
        },
        "spec": spec,
    }


def route_manifests(cfg: HarnessConfig, name: str, owner: dict[str, str], names: dict[str, int]) -> list[dict[str, Any]]:
    """One HTTPRoute per host name, owned by the claim. One each, not one with every name, so
    the ingress pools each name's connections apart; the gateway routes a connection by its
    first request's name."""
    namespace, _, rest = cfg.session_route_parent.partition("/")
    gateway, _, listener = rest.partition("/")
    service, _, port = cfg.session_route_backend.rpartition(":")
    if not (namespace and gateway and service and port.isdigit()):
        raise SessionError("session_route_parent is NAMESPACE/GATEWAY[/LISTENER] and session_route_backend "
                           f"SERVICE:PORT (got {cfg.session_route_parent!r}, {cfg.session_route_backend!r}).")
    parent = {"group": "gateway.networking.k8s.io", "kind": "Gateway", "name": gateway, "namespace": namespace}
    if listener:
        parent["sectionName"] = listener
    routes = []
    for host in names:
        routes.append({
            "apiVersion": ROUTE_API,
            "kind": "HTTPRoute",
            "metadata": {
                "name": host.split(".", 1)[0],
                "labels": {LABEL_MANAGED_BY: MANAGED_BY, LABEL_SESSION: label_value(name)},
                "ownerReferences": [{"apiVersion": API_VERSION, "kind": "SandboxClaim", "name": owner["name"],
                                     "uid": owner["uid"]}],
            },
            "spec": {
                "parentRefs": [parent],
                "hostnames": [host],
                # A UI holds event streams open; the ingress's default would cut them.
                "rules": [{"backendRefs": [{"name": service, "port": int(port)}], "timeouts": {"request": "0s"}}],
            },
        })
    return routes


def _get(cfg: HarnessConfig, kind: str, name: str) -> dict[str, Any] | None:
    raw = _kubectl(cfg, "get", kind, name, "-o", "json", "--ignore-not-found")
    return json.loads(raw) if raw else None


def _sandbox(cfg: HarnessConfig, claim: dict[str, Any]) -> str:
    """The claim's Sandbox (and so its pod's) name, once it has one."""
    for _ in range(60):
        if sandbox := (claim.get("status") or {}).get("sandbox", {}).get("name"):
            return str(sandbox)
        time.sleep(2)
        claim = _get(cfg, "sandboxclaim", claim["metadata"]["name"]) or claim
    raise SessionError(f"SandboxClaim {claim['metadata']['name']} has no sandbox yet.")


def state(cfg: HarnessConfig, name: str) -> str:
    """running, suspended, or absent."""
    claim = _get(cfg, "sandboxclaim", claim_name(name))
    if claim is None:
        return "absent"
    sandbox_name = (claim.get("status") or {}).get("sandbox", {}).get("name")
    sandbox = _get(cfg, "sandbox", sandbox_name) if sandbox_name else None
    if sandbox is not None and (sandbox.get("spec") or {}).get("operatingMode") == "Suspended":
        return "suspended"
    return "running"


def _set_mode(cfg: HarnessConfig, sandbox: str, mode: str) -> None:
    _kubectl(cfg, "patch", "sandbox", sandbox, "--type", "merge", "-p", json.dumps({"spec": {"operatingMode": mode}}))


def _wait_gone(cfg: HarnessConfig, pod: str) -> None:
    """Until the pod is deleted (at once if there is none)."""
    try:
        _kubectl(cfg, "wait", "--for=delete", f"pod/{pod}", "--timeout=300s")
    except AgentSandboxError as err:
        raise SessionError(f"Pod {pod} did not go: {err}") from err


def _pod_address(cfg: HarnessConfig, pod: str, timeout: float = 300) -> str:
    """The address of the pod once it is up and ready, and not one on its way out."""
    deadline = time.monotonic() + timeout
    while True:
        found = json.loads(_kubectl(cfg, "get", "pod", pod, "--ignore-not-found", "-o", "json") or "{}")
        if found.get("metadata", {}).get("deletionTimestamp"):
            _wait_gone(cfg, pod)
            continue
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (found.get("status") or {}).get("conditions") or [])
        if ready and (address := (found.get("status") or {}).get("podIP")):
            return address
        if time.monotonic() > deadline:
            raise SessionError(f"Pod {pod} was not ready in {int(timeout)}s.")
        time.sleep(2)


def _exec(target: KubectlTarget, argv: tuple[str, ...], *, env: dict[str, str] | None = None, detach: bool = False,
          workdir: str = WORKSPACE) -> subprocess.CompletedProcess[str]:
    """Run argv in the pod, in `workdir`, with HOME on the workspace and `env` added.

    `kubectl exec` has no way to set a variable, and a command line is no place
    for a password, so the variables go in on stdin (base64, one per line)."""
    lines = "".join(f"{key}={base64.b64encode(value.encode()).decode()}\n"
                    for key, value in {"HOME": HOME, **(env or {})}.items()) + "\n"
    script = ('while IFS= read -r line && [ -n "$line" ]; do '
              'export "${line%%=*}=$(printf %s "${line#*=}" | base64 -d)"; done; '
              'mkdir -p "$HOME" && cd "$0" || exit 1; '
              + ('setsid "$@" </dev/null >/dev/null 2>&1 & exit 0' if detach else 'exec "$@"'))
    return subprocess.run([*target.exec_prefix(stdin=True), "sh", "-c", script, workdir, *argv],
                          input=lines, capture_output=True, text=True, timeout=300)


def _opencode_api(target: KubectlTarget, password: str) -> Callable[[str, str, dict[str, Any] | None], Any]:
    """opencode's server in the pod, called with its curl. The password goes in on
    stdin and from there to curl's config on its stdin: never on a command line."""
    script = ('printf \'user = "%s:%s"\\n\' "$UI_USER" "$OPENCODE_SERVER_PASSWORD" | '
              f'curl -sS -f -m 10 -K - -X "$0" -H "Content-Type: application/json" '
              f'${{BODY:+--data-binary}} ${{BODY:+"$BODY"}} "http://127.0.0.1:{UI_PORT}$1"')

    def call(method: str, path: str, body: dict[str, Any] | None) -> Any:
        env = {"UI_USER": UI_USER, "OPENCODE_SERVER_PASSWORD": password}
        if body is not None:
            env["BODY"] = json.dumps(body)
        proc = _exec(target, ("sh", "-c", script, method, path), env=env, workdir="/")
        if proc.returncode != 0:
            raise SessionError(proc.stderr.strip() or f"curl exited {proc.returncode}")
        return json.loads(proc.stdout)
    return call


def _wait_for(target: KubectlTarget, port: int, timeout: int = 60) -> bool:
    """True once something answers HTTP on that port in the pod (a 401 counts)."""
    probe = (f"i=0; while [ $i -lt {timeout * 2} ]; do curl -s -o /dev/null -m 2 http://127.0.0.1:{port}/ && exit 0; "
             "i=$((i+1)); sleep 0.5; done; exit 1")
    return target.run_shell(probe, check=False, cwd="/").returncode == 0


def start(cfg: HarnessConfig, name: str, **options: Any) -> str:
    record, notes, how = launch(cfg, name, **options)
    head = f"Session {name} is {how} ({claim_name(name)} in {cfg.namespace}, egress {record['egress']} via the gateway)."
    return "\n".join([head, *notes, describe(record)])


def launch(
    cfg: HarnessConfig, name: str, *, config_file: str = "", terminal: bool | None = None,
    idle: str | None = None, lifetime: str | None = None, agent: str | None = None,
    allow: tuple[str, ...] | None = None, port: int = 0, bind: str = "", terminal_port: int = 0,
) -> tuple[dict[str, Any], list[str], str]:
    """Claim or resume the session, register it, and start its UI. Returns (its record,
    notes for the user, and how it is: "up" if claimed, "resumed" if it was
    suspended, "running" if it was already, its UI started again)."""
    claim = claim_name(name)
    refused = [flag for flag, given in (("--port", port), ("--bind", bind),
                                        ("--terminal-port", terminal_port), ("--root", cfg.container_root)) if given]
    if refused:
        raise SessionError(f"{', '.join(refused)}: not for a session on a cluster (yet). See harness/cluster_session.py.")
    missing = [key for key in ("pool", "namespace", "gateway_control", "session_domain", "session_route_parent")
               if not getattr(cfg, key)]
    if missing:
        raise SessionError(f"A session on a cluster needs {', '.join(missing)} set.")
    gateway = Gateway.from_config(cfg)
    assert gateway is not None
    record = load_record(cfg, name)
    agent = agent or record.get("agent") or "opencode"
    if agent not in AGENTS:
        raise SessionError(f"A session's agent is one of {', '.join(AGENTS)} (got {agent!r}).")
    claude = agent == "claude-code"
    allowed = list(dict.fromkeys(allow if allow is not None else record.get("allow") or []))
    if terminal is None:
        terminal = bool(record.get("terminal"))
    terminal = terminal or claude

    if claude:
        config, endpoint = None, ()
    elif config_file:
        try:
            config = json.loads(Path(config_file).expanduser().read_text())
            if not isinstance(config, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as err:
            raise SessionError(f"{config_file}: {err}") from err
        endpoint = ()
    else:
        config = opencode_config(cfg)
        endpoint = model_endpoint(config)
    if not claude:
        endpoint = (*endpoint, *OPENCODE_HOSTS)

    cfg = replace(cfg, egress="log" if cfg.egress == "open" else cfg.egress, engine="opencode")
    names = hosts(cfg, name, claude=claude, terminal=terminal)
    # Worked out before claiming, so a setting that cannot be used claims nothing.
    try:
        idle_after = seconds(idle) if idle is not None else int(record.get("idle") or 0)
        body, summary = registration(cfg, address="", ttl=0, allow=(*endpoint, *allowed), inbound=names,
                                     idle=idle_after)
    except ContainerError as err:
        raise SessionError(str(err)) from err

    existing = _get(cfg, "sandboxclaim", claim)
    how = "up" if existing is None else "running"
    limit = seconds(lifetime) if lifetime is not None else int(record.get("lifetime") or 0)
    if existing is None:
        existing = json.loads(_kubectl(cfg, "create", "-f", "-", "-o", "json",
                                       stdin=json.dumps(claim_manifest(cfg, name, lifetime=limit))))
    elif lifetime is not None:
        raise SessionError(f"Session {name} exists: --lifetime is set when it is first claimed. "
                           "`session rm` it to start afresh.")
    sandbox = _sandbox(cfg, existing)
    if (_get(cfg, "sandbox", sandbox) or {}).get("spec", {}).get("operatingMode") == "Suspended":
        _wait_gone(cfg, sandbox)  # a stop that is still going
        _set_mode(cfg, sandbox, "Running")
        how = "resumed"
    address = _pod_address(cfg, sandbox)
    target = KubectlTarget(repo=f"{WORKSPACE}/repo" if cfg.repo_url else WORKSPACE, pod=sandbox,
                           namespace=cfg.namespace, container=cfg.container or None,
                           context=cfg.kube_context or None, kubectl=cfg.kubectl)
    deadline = (existing.get("spec") or {}).get("lifecycle", {}).get("shutdownTime")
    ttl = 0
    if deadline:
        left = datetime.strptime(deadline, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
        ttl = max(60, int(left.total_seconds()))
    body.update(address=address, ttl=ttl)
    try:
        gateway.register(claim, body)
    except GatewayError as err:
        raise SessionError(str(err)) from err

    needed = (*REQUIRED_COMMANDS, "curl", "claude" if claude else "opencode", *(("zellij", "socat") if terminal else ()))
    if absent := target.missing_commands(needed):
        raise SessionError(f"{sandbox}'s image is missing: {', '.join(absent)}. See images/sandbox/Containerfile.")
    notes = [f"Egress: {summary}."]
    if cfg.repo_url:
        if git.is_repo(target):
            notes.append(f"Using the checkout already in {target.repo}.")
        else:
            auth = () if "git" in cfg.egress_inject else git.auth_args(cfg.repo_url)
            notes.append(git.clone(target, cfg.repo_url, auth=auth))

    password = record.get("password") or secrets.token_urlsafe(18)
    link = ""
    # Whatever ran in this pod before (a session started again while running) goes first.
    _exec(target, ("sh", "-c", "pkill -f 'opencode web' ; pkill -f 'zellij web' ; pkill -f 'socat TCP-LISTEN' ; true"))
    if claude:
        # Claude Code's config is composed in $HOME, which here is on the workspace.
        _exec(target, ("burst-sandbox-init",))
        if not claude_env(cfg):
            notes.append("No Claude Code credential on the driver: log in from the terminal, or start the session "
                         "again with one set.")
    else:
        if config is not None:
            target.run_shell(f"mkdir -p {shlex.quote(str(Path(OPENCODE_CONFIG).parent))} && "
                             f"cat > {shlex.quote(OPENCODE_CONFIG)}", stdin=json.dumps(config, indent=2) + "\n", cwd="/")
        else:
            notes.append(f"No model configured for opencode: provider={cfg.provider} is not one the harness can "
                         "describe to it. Set one up in the UI, or pass --opencode-config FILE.")
        serve = f"exec opencode web --hostname 0.0.0.0 --port {UI_PORT} >> /tmp/opencode.log 2>&1"
        proc = _exec(target, ("sh", "-c", serve), env={"OPENCODE_SERVER_PASSWORD": password, **model_env(cfg)},
                     detach=True, workdir=target.repo)
        if proc.returncode != 0 or not _wait_for(target, UI_PORT):
            log = target.run_shell("tail -n 20 /tmp/opencode.log", check=False, cwd="/").stdout.strip()
            raise SessionError(f"opencode's UI did not come up in {sandbox}. {proc.stderr.strip()}\n{log}")
        link = open_in(_opencode_api(target, password), target.repo, name, notes)
    token = ""
    if terminal:
        env = claude_env(cfg) if claude else {}
        token = terminal_in(lambda *argv, detach=False: _exec(target, argv, env=env, detach=detach,
                                                             workdir=target.repo), claude=claude)
        if not _wait_for(target, TERMINAL_PORT):
            log = target.run_shell("tail -n 20 /tmp/zellij-web.log", check=False, cwd="/").stdout.strip()
            raise SessionError(f"The terminal did not come up in {sandbox}.\n{log}")

    owner = {"name": claim, "uid": existing["metadata"]["uid"]}
    for route in route_manifests(cfg, name, owner, names):
        _kubectl(cfg, "apply", "-f", "-", stdin=json.dumps(route))

    record = {
        "name": name, "target": "agent-sandbox", "namespace": cfg.namespace, "claim": claim, "hosts": names,
        "password": password, "repo_url": git.redact_url(cfg.repo_url), "egress": cfg.egress,
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "lifetime": limit,
        "idle": idle_after,
        "agent": agent, "allow": allowed, "link": link,
    }
    if terminal:
        record["terminal"] = {"token": token, "path": terminal_session(claude)}
    save_record(cfg, name, record)
    return record, notes, how


def urls(record: dict[str, Any]) -> dict[str, str]:
    by_port = {port: f"https://{host}/" for host, port in record["hosts"].items()}
    found = {}
    if terminal := record.get("terminal"):
        found["terminal"] = by_port[TERMINAL_PORT] + (terminal.get("path") or "")
    found["ui"] = found["terminal"] if record.get("agent") == "claude-code" else by_port[UI_PORT] + record.get("link", "")
    return found


def describe(record: dict[str, Any]) -> str:
    where = urls(record)
    if record.get("agent") == "claude-code":
        lines = [f"  UI:        {where['ui']}  (Claude Code, in a terminal)", f"  token:     {record['terminal']['token']}"]
    else:
        lines = [f"  UI:        {where['ui']}", f"  user:      {UI_USER}", f"  password:  {record['password']}"]
    if record.get("allow"):
        lines.append(f"  allows:    {', '.join(record['allow'])} (as well as the defaults)")
    if record.get("idle"):
        lines.append(f"  suspended: after {record['idle']}s with nothing going in or out (`harness session reap`)")
    if record.get("lifetime"):
        lines.append(f"  deleted:   {record['lifetime']}s after it was first claimed")
    if (terminal := record.get("terminal")) and record.get("agent") != "claude-code":
        lines += [f"  terminal:  {where['terminal']}", f"  token:     {terminal['token']}"]
    return "\n".join(lines)


def _unregister(cfg: HarnessConfig, name: str) -> str:
    """Take the session off the gateway, saving what it did. A line saying so, or why not."""
    from .containers import events_path

    try:
        gateway = Gateway.from_config(cfg)
        if gateway is None:
            return ""
        events = gateway.unregister(claim_name(name))
    except GatewayError as err:
        return "" if "404" in str(err) else f" Could not unregister it from the gateway ({err})."
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return save_events(events_path(cfg, f"{claim_name(name)}-{stamp}"), events)


def stop(cfg: HarnessConfig, name: str) -> str:
    """Suspend the session: its pod is deleted, its workspace kept."""
    claim = _get(cfg, "sandboxclaim", claim_name(name))
    if claim is None:
        raise SessionError(f"No session named {name} in {cfg.namespace}.")
    saved = _unregister(cfg, name)
    sandbox = _sandbox(cfg, claim)
    _set_mode(cfg, sandbox, "Suspended")
    _wait_gone(cfg, sandbox)
    return f"Stopped session {name}; its workspace is kept.{saved}"


def reap(cfg: HarnessConfig, now: float | None = None) -> str:
    """Suspend every session the gateway says has been idle for longer than its `--idle`."""
    gateway = Gateway.from_config(cfg)
    if gateway is None:
        raise SessionError("Reaping sessions needs gateway_control set.")
    now = time.time() if now is None else now
    lines = []
    for sandbox in gateway.sandboxes():
        claim, idle = str(sandbox.get("name") or ""), int(sandbox.get("idle") or 0)
        quiet = now - float(sandbox.get("active") or now)
        if not idle or quiet < idle or not claim.startswith("session-"):
            continue
        name = claim.removeprefix("session-")
        found = _get(cfg, "sandboxclaim", claim)
        if found is None or (found["metadata"].get("labels") or {}).get(LABEL_SESSION) != name:
            continue  # not a session of ours, whatever its name
        try:
            lines.append(f"{stop(cfg, name)} (idle {int(quiet)}s, --idle {idle}s)")
        except (SessionError, AgentSandboxError) as err:
            lines.append(f"Could not stop session {name}: {err}")
    return "\n".join(lines) or "No session has been idle too long."


def remove(cfg: HarnessConfig, name: str) -> str:
    """Delete the claim, and with it the sandbox, its workspace and its routes."""
    saved = _unregister(cfg, name)
    _kubectl(cfg, "delete", "sandboxclaim", claim_name(name), "--ignore-not-found", "--wait=false")
    record_path(cfg, name).unlink(missing_ok=True)
    return f"Removed session {name}: its claim, workspace and routes are deleted.{saved}"


def list_sessions(cfg: HarnessConfig) -> list[dict[str, str]]:
    claims = json.loads(_kubectl(cfg, "get", "sandboxclaims", "-l", LABEL_SESSION, "-o", "json")).get("items", [])
    modes = {}
    for sandbox in json.loads(_kubectl(cfg, "get", "sandboxes", "-o", "json")).get("items", []):
        modes[sandbox["metadata"]["name"]] = (sandbox.get("spec") or {}).get("operatingMode") or "Running"
    rows = []
    for claim in claims:
        name = claim["metadata"]["labels"][LABEL_SESSION]
        record = load_record(cfg, name)
        sandbox = (claim.get("status") or {}).get("sandbox", {}).get("name", "")
        running = modes.get(sandbox, "Running") != "Suspended"
        rows.append({
            "name": name,
            "status": "running" if running else "stopped",
            "agent": record.get("agent") or "opencode",
            "ui": urls(record)["ui"] if running and record.get("hosts") else "-",
            "repo": record.get("repo_url") or "-",
        })
    return sorted(rows, key=lambda row: row["name"])
