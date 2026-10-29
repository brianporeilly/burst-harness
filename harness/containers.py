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
443. On podman the sidecar is also the network's default route, and redirects
whatever arrives into itself, so software that ignores the proxy is held to the
same policy and recorded like the rest. With `egress_inject` the sidecar also
terminates TLS to the repo's host and adds the git token there, so the
container can clone and push without ever holding it; and likewise to the model
API's host, for an engine that runs in the container. Either way every decision is an event on the sidecar's stdout, so `podman
logs <name>-egress` is the audit trail, saved under `state_dir`/egress when the
sidecar and network are removed with the container.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import credentials, git, mcp
from .agent_sandbox import (
    LABEL_MANAGED_BY,
    LABEL_TASK,
    MANAGED_BY,
    REQUIRED_COMMANDS,
    label_value,
)

# On the containers of a `harness session`: the session's name.
LABEL_SESSION = "burst-harness/session"
from .config import HarnessConfig
from .exec_target import DockerTarget


class ContainerError(RuntimeError):
    """The container could not be started or lacks required commands."""


PROXY_PORT = 3128
REDIRECT_PORT = 3129
DNS_PORT = 53
EGRESS_MODES = ("open", "log", "allowlist")
# Where the gateway makes its CA, and where burst-sandbox-trust leaves it in the sandbox.
GATEWAY_CA_DIR = "/run/burst-gateway"
SANDBOX_CA_BUNDLE = "/etc/burst-harness/ca-bundle.crt"
SANDBOX_CA = "/etc/burst-harness/gateway-ca.crt"
# egress_inject = model. For each credential Claude Code reads from its
# environment: the header it is sent in, and what the sandbox is given in its
# place. The placeholders keep the real ones' prefixes, in case Claude Code
# tells an API key from an OAuth token by looking.
MODEL_CREDENTIALS = {
    "ANTHROPIC_API_KEY": ("x-api-key", "sk-ant-api03-burst-gateway-placeholder-api-key"),
    "CLAUDE_CODE_OAUTH_TOKEN": ("Authorization", "sk-ant-oat01-burst-gateway-placeholder-oauth-token"),
    "ANTHROPIC_AUTH_TOKEN": ("Authorization", "burst-gateway-placeholder-auth-token"),
}
DEFAULT_MODEL_API = "https://api.anthropic.com"
# Engines whose agent loop, and so whose model calls, run in the container.
# "opencode" is what session.py passes: it is not an --engine for tasks.
IN_TARGET_ENGINES = ("claude-code", "opencode")
# The runtime's default network, which has a route out.
DEFAULT_NETWORK = {"podman": "podman", "docker": "bridge"}


def _labels(slug: str) -> list[str]:
    return ["--label", f"{LABEL_MANAGED_BY}={MANAGED_BY}", "--label", f"{LABEL_TASK}={label_value(slug)}"]


# What root needs to install packages (chown and write files it does not own,
# create users and switch to them in scriptlets, set file capabilities), and
# nothing that reaches outside the container: no network or system administration.
ROOT_CAPABILITIES = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETUID", "SETGID", "SETFCAP")


def check_root(cfg: HarnessConfig, binary: str) -> None:
    """Refuse container_root where root in the container would be root on the host."""
    if not cfg.container_root:
        return
    rootless = binary == "podman" and _run(binary, "info", "--format", "{{.Host.Security.Rootless}}") == "true"
    if not rootless:
        raise ContainerError(
            "--root needs rootless podman: there root in the container is your own user outside it. "
            f"With {binary} here it would be root on this machine."
        )


def run_argv(cfg: HarnessConfig, *, binary: str, name: str, slug: str, network_args: list[str] | tuple[str, ...] = ()) -> list[str]:
    # Nothing in the sandbox needs privileges: git, a shell, the agent's tools.
    # With container_root, root gets back only what installing packages takes.
    privileges = ["--cap-drop=ALL", "--security-opt=no-new-privileges"]
    if cfg.container_root:
        privileges += ["--user", "0", *(f"--cap-add={cap}" for cap in ROOT_CAPABILITIES)]
    return [
        binary, "run", "--detach", "--name", name, *_labels(slug),
        *privileges,
        *network_args,
        *shlex.split(cfg.container_args),
        cfg.image,
    ]


def approved_path(cfg: HarnessConfig) -> Path:
    """Hosts approved for good with `harness egress`: added to every allowlist from then on."""
    return Path(cfg.state_dir).expanduser() / "egress" / "approved.json"


def approved(cfg: HarnessConfig) -> list[str]:
    path = approved_path(cfg)
    try:
        return [str(host) for host in json.loads(path.read_text()).get("allow", [])] if path.is_file() else []
    except (OSError, ValueError, AttributeError) as err:
        raise ContainerError(f"{path}: {err}") from err


def allowlist(cfg: HarnessConfig, extra: tuple[str, ...] = ()) -> list[str]:
    """egress_allow, what has been approved for good, and the hosts this run needs to reach no matter what."""
    hosts = list(dict.fromkeys((*cfg.egress_allow, *approved(cfg), *extra, *mcp.hosts(cfg))))
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


def inject_rules(cfg: HarnessConfig) -> list[dict[str, object]]:
    """The gateway's EGRESS_INJECT rules for `egress_inject`: where to add which credential."""
    rules: list[dict[str, object]] = []
    for what in cfg.egress_inject:
        if what == "model":
            rules.append(model_rule(cfg))
            continue
        if what != "git":
            raise ContainerError(f"--egress-inject takes: git, model (got {what!r}).")
        slug = git.repo_slug(cfg.repo_url)
        if not slug or not cfg.repo_url.startswith("https://"):
            raise ContainerError("--egress-inject git needs an https:// --repo-url to add the token to.")
        if not (basic := git.basic_credential()):
            raise ContainerError(
                f"--egress-inject git needs a token on the driver: one of {', '.join(git.TOKEN_ENV_VARS)}."
            )
        host, _, path = slug.partition("/")
        # Only this repo's paths, with and without the .git suffix: the token
        # is not offered to the rest of the host.
        rules += [{"host": host, "path": f"/{path}{suffix}/*", "headers": {"Authorization": f"Basic {basic}"}}
                  for suffix in (".git", "")]
    return [*rules, *mcp.inject_rules(cfg)]


def model_rule(cfg: HarnessConfig) -> dict[str, object]:
    """Swap the placeholders the sandbox is given for the driver's model credentials, at the API's host."""
    if cfg.engine not in IN_TARGET_ENGINES:
        raise ContainerError(
            "--egress-inject model is for an engine that runs in the container (claude-code, or a session's "
            f"opencode); with engine={cfg.engine} the model is called from the driver and its key never leaves."
        )
    api = urlsplit(os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_MODEL_API)
    if api.scheme != "https" or not api.hostname or (api.port or 443) != 443:
        raise ContainerError(f"--egress-inject model needs ANTHROPIC_BASE_URL to be https:// on port 443 (got {api.geturl()}).")
    swap = [{"header": header, "placeholder": placeholder, "value": credentials.get(name)}
            for name, (header, placeholder) in MODEL_CREDENTIALS.items() if credentials.get(name)]
    if not swap:
        raise ContainerError(
            f"--egress-inject model needs a credential on the driver: one of {', '.join(MODEL_CREDENTIALS)}."
        )
    return {"host": api.hostname, "swap": swap}


def trust_gateway(binary: str, *, proxy: str, name: str) -> None:
    """Have the sandbox trust its gateway's CA, which it needs before the gateway can terminate TLS for it."""
    path = f"{GATEWAY_CA_DIR}/ca.crt"
    for _ in range(100):  # the gateway makes it at start-up
        proc = subprocess.run([binary, "exec", proxy, "cat", path], capture_output=True, text=True)
        if proc.returncode == 0 and "END CERTIFICATE" in proc.stdout:
            break
        time.sleep(0.1)
    else:
        raise ContainerError(f"{proxy} did not produce {path}: {proc.stderr.strip()}")
    done = subprocess.run([binary, "exec", "-i", "--user", "0", name, "burst-sandbox-trust"],
                          input=proc.stdout, capture_output=True, text=True)
    if done.returncode != 0:
        raise ContainerError(
            f"{name} could not trust the gateway's CA ({done.stderr.strip() or 'burst-sandbox-trust failed'}). "
            "--egress-inject needs burst-sandbox-trust in the image; see images/sandbox/."
        )


def _run(binary: str, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run([binary, *args], capture_output=True, text=True, env={**os.environ, **env} if env else None)
    if proc.returncode != 0:
        raise ContainerError(f"{binary} {' '.join(args[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _routed_network(binary: str, network: str) -> tuple[str, str]:
    """Create `network` routing everything to one address on it. Returns (subnet, that address)."""
    # The route is fixed at creation and needs an address in a subnet podman
    # has not chosen yet: let it choose, then recreate the network with the route.
    create = ["network", "create", "--internal", "--disable-dns"]
    _run(binary, *create, network)
    subnet = _run(binary, "network", "inspect", network, "--format", "{{range .Subnets}}{{.Subnet}}{{end}}")
    _run(binary, "network", "rm", network)
    gateway = str(ipaddress.ip_network(subnet)[-2])
    # A worse metric than the gateway's own default route, which it has to keep.
    _run(binary, *create, "--subnet", subnet, "--route", f"0.0.0.0/0,{gateway},500", network)
    return subnet, gateway


def start_egress(
    cfg: HarnessConfig, *, binary: str, name: str, slug: str,
    allow: tuple[str, ...] = (), inbound: tuple[tuple[str, int, int], ...] = (), labels: tuple[str, ...] = (),
    gateway_env: tuple[str, ...] = (), inject: tuple[dict[str, object], ...] = (),
    read_only: tuple[dict[str, object], ...] | None = None, upstream_ca: tuple[dict[str, str], ...] = (),
) -> tuple[str, str, list[str]]:
    """Create the internal network and gateway sidecar.

    `allow` adds to the allowlist, and `inject` to the EGRESS_INJECT rules
    `egress_inject` makes. `inbound` is (host address, host port,
    container port): the gateway publishes each on the host and passes what
    arrives to the container. `labels` are extra `--label` flags, `gateway_env`
    extra NAME=VALUE settings for the gateway. `read_only`, when given, makes
    the gateway refuse every request that writes except those it lists
    (EGRESS_WRITES rules); it then terminates all TLS, so the sandbox has to
    trust its CA (trust_gateway). `upstream_ca` is EGRESS_UPSTREAM_CA entries,
    for hosts whose certificates are not signed by a public CA.

    Returns (network, proxy, run flags that put a container behind them).
    """
    if inbound and binary != "podman":
        raise ContainerError("Passing connections in to a container needs podman (it has to have a fixed address).")
    if cfg.egress == "allowlist":
        hosts = allowlist(cfg, allow)
        policy, summary = ["-e", f"EGRESS_ALLOW={','.join(hosts)}"], f"allowlist: {', '.join(hosts)}"
        if cfg.egress_webhook:
            policy += ["-e", f"EGRESS_WEBHOOK={cfg.egress_webhook}"]
    else:
        check_reachable(cfg)
        policy, summary = [], "everything passes and is recorded"
    secrets_env: dict[str, str] = {}
    # The gateway's one writable place: its CA when it adds credentials, and
    # approvals made while it runs (`harness egress approve`).
    policy += ["--tmpfs", f"{GATEWAY_CA_DIR}:rw,mode=1777"]
    if rules := [*inject_rules(cfg), *inject]:
        # By name only: the value comes from this process's environment, so the
        # token is not in the argv of the command that starts the gateway.
        secrets_env["EGRESS_INJECT"] = json.dumps(rules)
        policy += ["-e", "EGRESS_INJECT"]
        summary += "; adds credentials for " + ", ".join(sorted({str(rule["host"]) for rule in rules}))
    if upstream_ca:
        secrets_env["EGRESS_UPSTREAM_CA"] = json.dumps(list(upstream_ca))
        policy += ["-e", "EGRESS_UPSTREAM_CA"]
    if read_only is not None:
        policy += ["-e", "EGRESS_READONLY=1", "-e", f"EGRESS_WRITES={json.dumps(list(read_only))}"]
        summary += "; read-only" + (" except " + ", ".join(f"{w['method']} {w['host']}{w.get('path', '')}"
                                                         for w in read_only) if read_only else "")
    network, proxy = f"{name}-net", f"{name}-egress"
    # Only podman can give a network a route (`network create --route`); on
    # docker the sandbox has no route at all and only the proxy gets out.
    redirect = binary == "podman"
    subnet = address = fixed = ""
    if redirect:
        subnet, address = _routed_network(binary, network)
        # The container's own address, fixed so the gateway can be told where to pass connections in.
        fixed = str(ipaddress.ip_network(subnet)[2])
        for host, host_port, port in inbound:
            policy += ["-p", f"{host}:{host_port}:{port}"]
        if inbound:
            policy += ["-e", "EGRESS_INBOUND=" + ",".join(f"{port}={fixed}:{port}" for _, _, port in inbound)]
        # NET_ADMIN is for burst-gateway-netinit, run as root below; the
        # gateway's own process is unprivileged and never holds it. With
        # forwarding off, what the rules do not redirect goes nowhere.
        policy += ["--cap-add=NET_ADMIN", "--sysctl", "net.ipv4.ip_forward=0", "-e", f"EGRESS_REDIRECT={REDIRECT_PORT}"]
    else:
        _run(binary, "network", "create", "--internal", network)
    try:
        _run(
            binary, "run", "--detach", "--name", proxy, *_labels(slug), *labels,
            "--network", DEFAULT_NETWORK.get(binary, "bridge"),
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
            # Lets the unprivileged gateway bind its resolver to port 53.
            "--sysctl", "net.ipv4.ip_unprivileged_port_start=0",
            "-e", f"EGRESS_MODE={cfg.egress}", "-e", f"EGRESS_LISTEN={PROXY_PORT}",
            "-e", f"EGRESS_SANDBOX={name}", *(arg for item in gateway_env for arg in ("-e", item)), *policy,
            cfg.gateway_image,
            env=secrets_env,
        )
        _run(binary, "network", "connect", *(["--ip", address] if address else []), network, proxy)
        if redirect:
            _run(binary, "exec", "--user", "0", proxy, "burst-gateway-netinit",
                 subnet, address, str(PROXY_PORT), str(REDIRECT_PORT), str(DNS_PORT))
        ip = address or _run(binary, "inspect", proxy, "--format",
                             "{{(index .NetworkSettings.Networks \"%s\").IPAddress}}" % network)
    except BaseException:  # Ctrl-C too: an unattended run is stopped that way
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
    if rules or read_only is not None:
        # Written by burst-sandbox-trust once the container is up.
        env += [f"{k}={SANDBOX_CA_BUNDLE}" for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO")]
        env.append(f"NODE_EXTRA_CA_CERTS={SANDBOX_CA}")
    # "." clears the host's search domains, which would otherwise be inherited.
    flags = ["--network", network, *(["--ip", fixed] if fixed else []), "--dns", ip, "--dns-search", "."]
    for item in env:
        flags += ["-e", item]
    print(f"Egress via {proxy} ({summary}).")
    return network, proxy, flags


def gateway_address(flags: list[str]) -> str:
    """The gateway's address on the container's network, from the run flags start_egress returned."""
    return flags[flags.index("--dns") + 1]


def acquire(
    cfg: HarnessConfig, *, binary: str, repo: str, slug: str,
    allow: tuple[str, ...] = (), inject: tuple[dict[str, object], ...] = (), env: dict[str, str] | None = None,
    read_only: tuple[dict[str, object], ...] | None = None, upstream_ca: tuple[dict[str, str], ...] = (),
) -> DockerTarget:
    """Attach to `cfg.docker_container`, or start a fresh one from `cfg.image`.

    `allow` and `inject` add to the gateway's allowlist and credential rules, and
    `read_only` limits it to reading (see start_egress). `env` is set in a fresh container's environment, passed by name
    so values stay out of argv.
    """
    if cfg.docker_container:
        return DockerTarget(repo=repo, container=cfg.docker_container, binary=binary)
    if not cfg.image:
        raise ContainerError(f"target={binary} needs --docker-container or --image.")
    if cfg.egress not in EGRESS_MODES:
        raise ContainerError(f"egress must be one of {', '.join(EGRESS_MODES)} (got {cfg.egress!r}).")
    if not shutil.which(binary):
        raise ContainerError(f"{binary} is not installed or not on PATH.")
    check_root(cfg, binary)
    name = f"burst-{label_value(slug)[:40]}-{secrets.token_hex(3)}"
    network = proxy = ""
    network_args: list[str] = []
    if (cfg.egress_inject or inject) and cfg.egress == "open":
        raise ContainerError("--egress-inject needs a gateway to do it: --egress log or allowlist.")
    if read_only is not None and cfg.egress == "open":
        raise ContainerError("Read-only needs a gateway to enforce it: egress log or allowlist.")
    if cfg.egress != "open":
        network, proxy, network_args = start_egress(cfg, binary=binary, name=name, slug=slug,
                                                    allow=allow, inject=inject, read_only=read_only,
                                                    upstream_ca=upstream_ca)
    env = env or {}
    target = DockerTarget(repo=repo, container=name, binary=binary, owned=True, proxy=proxy, network=network,
                          events=events_path(cfg, name) if proxy else "")
    try:
        proc = subprocess.run(
            run_argv(cfg, binary=binary, name=name, slug=slug,
                     network_args=[*network_args, *(arg for key in env for arg in ("-e", key))]),
            capture_output=True, text=True, env={**os.environ, **env} if env else None,
        )
    except BaseException:
        target.release()
        raise
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
        if proxy and (cfg.egress_inject or inject or read_only is not None or mcp.inject_rules(cfg)):
            trust_gateway(binary, proxy=proxy, name=name)
    except BaseException:
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
