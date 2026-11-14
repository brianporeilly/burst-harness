"""Runners: hosts of your own that work runs on, reached over SSH.

Each is listed in `runners` as NAME=URL, and `runner = NAME` (`--runner`) picks one, which also picks the target:

    runners = [
        "vm1=ssh://agent@vm1.example.com",                                       # a plain host: target ssh
        "box=podman+ssh://agent@box.example.com/run/user/1000/podman/podman.sock",  # its podman: target podman
        "big=docker+ssh://agent@big.example.com",                                 # its docker: target docker
        "here=podman+unix:///run/user/1000/podman/podman.sock",                   # an engine's socket, no SSH
    ]

**A plain host** (ssh://) is a target of its own: each run makes a directory under the URL's path (`burst` in the
account's home when it has none), clones the repo there, and works through `ssh` (ssh_exec.py); the directory is
removed when the run is done with it, or kept, like a sandbox, while it holds work not yet pushed
(`--runner-workdir` attaches to it again). There is nothing between it and its network, and the agent's
credentials are written there for the run, so it is for a machine given over to this: a VM reset now and then,
behind a firewall of its own. The harness refuses a gateway's settings (egress log or allowlist,
egress_inject) for it rather than seem to apply them.

**A container engine** (podman+ssh://, docker+ssh://) is the podman or docker target with the engine on the
runner: the harness forwards the engine's socket over SSH (`ssh -L`) for the run and points podman or docker
at it (CONTAINER_HOST, DOCKER_HOST), so the container and its gateway sidecar run there, with the same
isolation as on this machine. The path is the engine's socket there: rootless podman's is
/run/user/UID/podman/podman.sock (`systemctl --user enable --now podman.socket`, and `loginctl enable-linger`
so it runs while nobody is logged in); docker's is /var/run/docker.sock when none is given. podman+unix:// and
docker+unix:// name a socket on this machine instead (one forwarded some other way, say). The images are pulled
on the runner, and the model's and the repo's hosts must be reachable from it.

SSH runs non-interactively (BatchMode) and checks the host's key. The credential <NAME>_SSH_KEY, a private key,
is used when it is set, and <NAME>_SSH_KNOWN_HOSTS, known_hosts lines for the host, in place of your own
known_hosts; without them, your own SSH setup (agent, ~/.ssh) applies. One connection is opened per run and
shared by its commands (ControlMaster).
"""

from __future__ import annotations

import atexit
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from . import credentials
from .exec_target import SubprocessTarget

if TYPE_CHECKING:
    from .config import HarnessConfig

NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
KINDS = {"ssh": "ssh", "podman+ssh": "podman", "docker+ssh": "docker", "podman+unix": "podman",
         "docker+unix": "docker"}
DEFAULT_WORKDIR = "burst"  # in the account's home
DOCKER_SOCKET = "/var/run/docker.sock"
ENGINE_HOST = {"podman": "CONTAINER_HOST", "docker": "DOCKER_HOST"}
# What a plain host needs for the harness's own commands (as agent_sandbox.REQUIRED_COMMANDS).
REQUIRED_COMMANDS = ("sh", "git", "base64", "mkdir", "dirname", "ls", "rm", "cat", "env")
TUNNEL_WAIT = 20.0
# The engine sockets this process has forwarded, by runner: one tunnel each, however often it is asked.
_forwarded: dict[str, str] = {}


class RunnerError(ValueError):
    """A `runners` entry that cannot be used, or a runner that cannot be reached."""


@dataclass(frozen=True)
class Runner:
    name: str
    kind: str  # ssh | podman | docker: the target it gives
    host: str = ""  # "" for a local socket
    user: str = ""
    port: int | None = None
    path: str = ""  # ssh: where work goes (relative: in the home); an engine: its socket

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def over_ssh(self) -> bool:
        return bool(self.host)

    @property
    def key_var(self) -> str:
        return f"{_env_name(self.name)}_SSH_KEY"

    @property
    def known_hosts_var(self) -> str:
        return f"{_env_name(self.name)}_SSH_KNOWN_HOSTS"


def _env_name(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", name.upper())


def parse(entry: str) -> Runner:
    """A `runners` entry, NAME=URL, checked."""
    name, sep, url = entry.partition("=")
    name, url = name.strip(), url.strip()
    if not sep or not NAME.fullmatch(name):
        raise RunnerError(f"A runners entry is NAME=URL, with a lowercase name (got {entry!r}).")
    parts = urlsplit(url)
    kind = KINDS.get(parts.scheme)
    if kind is None:
        raise RunnerError(f"runners {name}: {url!r} is not ssh://, podman+ssh://, docker+ssh://, podman+unix:// "
                          "or docker+unix://.")
    path = unquote(parts.path)
    if parts.scheme.endswith("+unix"):
        if parts.netloc or not path.startswith("/"):
            raise RunnerError(f"runners {name}: {url!r} names a socket on this machine: {parts.scheme}:///path.")
        return Runner(name, kind, path=path)
    try:
        port = parts.port
    except ValueError as err:
        raise RunnerError(f"runners {name}: {url!r} has a port that is not a number.") from err
    host = parts.hostname or ""
    if not host or parts.password or parts.query or parts.fragment:
        raise RunnerError(f"runners {name}: {url!r} is not {parts.scheme}://[user@]host[:port][/path].")
    if kind == "ssh":
        path = path.rstrip("/") or DEFAULT_WORKDIR
        if path.startswith("/~/"):
            path = path[3:]  # ssh://host/~/work: in the home
    elif kind == "podman" and not path.startswith("/"):
        raise RunnerError(f"runners {name}: podman+ssh needs the socket's path on the host, such as "
                          "/run/user/1000/podman/podman.sock.")
    elif kind == "docker":
        path = path if path.startswith("/") else DOCKER_SOCKET
    if any(c in path for c in "\n\0"):
        raise RunnerError(f"runners {name}: {url!r} has a path that cannot be used.")
    return Runner(name, kind, host=host, user=unquote(parts.username or ""), port=port, path=path)


def runners(cfg: HarnessConfig) -> list[Runner]:
    """`runners`, each checked, with no name listed twice."""
    found: list[Runner] = []
    for entry in cfg.runners:
        runner = parse(entry)
        if any(r.name == runner.name for r in found):
            raise RunnerError(f"runners {runner.name}: listed twice.")
        found.append(runner)
    return found


def find(cfg: HarnessConfig, name: str) -> Runner:
    for runner in runners(cfg):
        if runner.name == name:
            return runner
    listed = ", ".join(r.name for r in runners(cfg)) or "none"
    raise RunnerError(f"No runner named {name!r} (runners: {listed}).")


def resolve(cfg: HarnessConfig) -> HarnessConfig:
    """Check `runners`, and with `runner`, make the target the one it gives."""
    runners(cfg)
    if not cfg.runner:
        if cfg.target == "ssh":
            raise RunnerError("target=ssh is a runner's: pick it with --runner NAME (runners lists them).")
        return cfg
    return replace(cfg, target=find(cfg, cfg.runner).kind)


# --- SSH -----------------------------------------------------------------------


@dataclass
class Connection:
    """ssh to one runner for this process: its options, the key and known_hosts it was given (files in a
    private directory), and the master connection its commands share. Closed at exit."""

    runner: Runner
    dir: str = ""
    argv: tuple[str, ...] = ()
    tunnels: list[subprocess.Popen[bytes]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Short, for the control socket's path (a unix socket's is limited to about 100 bytes).
        self.dir = tempfile.mkdtemp(prefix="burst-ssh-", dir="/tmp" if os.path.isdir("/tmp") else None)
        options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
                   "-o", "ServerAliveCountMax=4", "-o", "StrictHostKeyChecking=yes",
                   "-o", "ControlMaster=auto", "-o", f"ControlPath={self.dir}/cm", "-o", "ControlPersist=120"]
        if key := credentials.get(self.runner.key_var):
            options += ["-i", self._private("key", key), "-o", "IdentitiesOnly=yes"]
        if known := credentials.get(self.runner.known_hosts_var):
            options += ["-o", f"UserKnownHostsFile={self._private('known_hosts', known)}"]
        if self.runner.port:
            options += ["-p", str(self.runner.port)]
        self.argv = ("ssh", *options, self.runner.destination)
        atexit.register(self.close)

    def _private(self, name: str, text: str) -> str:
        path = os.path.join(self.dir, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(text.strip() + "\n")
        return path

    def close(self) -> None:
        for tunnel in self.tunnels:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
        self.tunnels.clear()
        if self.dir and os.path.exists(os.path.join(self.dir, "cm")):
            subprocess.run([*self.argv[:-1], "-O", "exit", self.argv[-1]], capture_output=True, timeout=15,
                           check=False)
        shutil.rmtree(self.dir, ignore_errors=True)


_connections: dict[Runner, Connection] = {}


def connection(runner: Runner) -> Connection:
    """The process's connection to `runner`, opened the first time it is asked for."""
    if runner not in _connections:
        _connections[runner] = Connection(runner)
    return _connections[runner]


def close_all() -> None:
    """Close every connection and forward this process opened (as happens at exit)."""
    for link in _connections.values():
        link.close()
    _connections.clear()
    _forwarded.clear()


# --- a plain host: target ssh --------------------------------------------------


@dataclass(frozen=True)
class SshTarget(SubprocessTarget):
    """Run on a runner's host over ssh, in a directory of its own (workdir) that the repo is cloned into."""

    ssh: tuple[str, ...] = ()
    runner: str = ""
    destination: str = ""
    workdir: str = ""
    owned: bool = False

    def exec_prefix(self, *, stdin: bool = False, tty: bool = False) -> list[str]:
        flags = [*(["-i"] if stdin else []), *(["-t"] if tty else [])]
        return [sys.executable, "-m", "harness.ssh_exec", *flags, *self.ssh, "--"]

    @property
    def scratch(self) -> str:
        # Not /tmp/burst-harness: other runs share the host, and their credentials go in here.
        return f"{self.workdir}/.burst" if self.workdir else super().scratch

    def describe(self) -> str:
        return f"SSH host {self.runner} ({self.destination}) at {self.repo}"

    def attach_hint(self) -> str:
        return f"--runner {self.runner} --runner-workdir {shlex.quote(self.workdir)}"

    def release(self) -> str:
        if not self.owned or not self.workdir.strip("/"):
            return ""
        removed = self.run_shell(f"rm -rf -- {shlex.quote(self.workdir)}", check=False, cwd="/")
        if removed.returncode != 0:
            return f"Could not remove {self.workdir} on {self.runner}: {(removed.stderr or removed.stdout).strip()}"
        return f"Removed {self.workdir} on {self.runner}."


def acquire(cfg: HarnessConfig, *, slug: str) -> SshTarget:
    """A directory on the runner `cfg.runner` (a plain host): `runner_workdir` again, or a fresh one."""
    from .agent_sandbox import label_value

    runner = find(cfg, cfg.runner)
    if runner.kind != "ssh":
        raise RunnerError(f"Runner {runner.name} is a {runner.kind} engine, not a plain host.")
    if cfg.egress != "open" or cfg.egress_inject:
        raise RunnerError(f"Runner {runner.name} is a plain host, with no gateway to keep its network to an "
                          "allowlist, record it, or add credentials: use egress open there (or a podman or "
                          "docker runner, which has one).")
    ssh = connection(runner).argv
    base = SshTarget(repo="/", ssh=ssh, runner=runner.name, destination=runner.destination)
    workdir = cfg.runner_workdir
    if workdir and base.run_shell(f"test -d {shlex.quote(workdir)}", check=False, cwd="/").returncode != 0:
        print(f"{workdir} is gone from {runner.name}: starting in a fresh directory. Anything it held that "
              "was not pushed is lost.")
        workdir = ""
    owned = not workdir
    if owned:
        home = base.run_shell('printf %s "$HOME"', cwd="/").stdout.strip()
        root = runner.path if runner.path.startswith("/") else f"{home.rstrip('/')}/{runner.path}"
        workdir = f"{root}/{label_value(slug)[:40]}-{secrets.token_hex(3)}"
        base.run_shell(f"umask 077 && mkdir -p {shlex.quote(workdir)}", cwd="/")
    target = replace(base, repo=f"{workdir}/repo", workdir=workdir, owned=owned)
    if missing := target.missing_commands(REQUIRED_COMMANDS):
        print(target.release())
        raise RunnerError(f"Runner {runner.name} is missing commands the harness needs: {', '.join(missing)}.")
    print(f"Working on {runner.name} ({runner.destination}) in {workdir}. A plain host: its network is its "
          "own, with no gateway.")
    return target


# --- a container engine: target podman or docker -------------------------------


def connect_engine(cfg: HarnessConfig) -> str:
    """Point podman or docker, for this process and what it starts, at `cfg.runner`'s engine. Returns the
    variable set ("" with no runner, or a plain host)."""
    if not cfg.runner:
        return ""
    runner = find(cfg, cfg.runner)
    if runner.kind == "ssh":
        return ""
    var = ENGINE_HOST[runner.kind]
    if not runner.over_ssh:
        os.environ[var] = f"unix://{runner.path}"
        return var
    if runner.name in _forwarded and os.path.exists(_forwarded[runner.name]):
        os.environ[var] = f"unix://{_forwarded[runner.name]}"
        return var
    link = connection(runner)
    local = os.path.join(link.dir, f"engine-{secrets.token_hex(3)}.sock")
    tunnel = subprocess.Popen(
        [*link.argv[:-1], "-N", "-o", "ExitOnForwardFailure=yes", "-o", "StreamLocalBindUnlink=yes",
         "-L", f"{local}:{runner.path}", link.argv[-1]],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    link.tunnels.append(tunnel)
    waited = time.monotonic() + TUNNEL_WAIT
    while not os.path.exists(local):
        if tunnel.poll() is not None or time.monotonic() > waited:
            err = (tunnel.stderr.read() if tunnel.poll() is not None and tunnel.stderr else b"").decode().strip()
            link.close()
            raise RunnerError(f"Could not reach {runner.kind} on runner {runner.name} ({runner.destination}:"
                              f"{runner.path}): {err or 'the forwarded socket never appeared'}")
        time.sleep(0.05)
    os.environ[var] = f"unix://{local}"
    _forwarded[runner.name] = local
    print(f"{runner.kind} on runner {runner.name} ({runner.destination}), its socket forwarded over SSH.")
    return var
