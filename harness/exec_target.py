"""Where commands run.

The harness executes two kinds of command: the agent's own shell and file edits
(through a Strands sandbox), and the harness's git and task-document operations
(plain synchronous subprocesses). Both must land in the *same* filesystem. If the
agent edits files in a pod while `git commit` runs on your laptop, the harness
commits an unchanged local directory and the real work is never recorded.

So a target owns both halves: `sandbox()` for the agent, `run()`/`read_file()` for
the harness. Choose one and everything follows it.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from strands.sandbox.base import Sandbox


class ExecError(RuntimeError):
    """A command failed in the target environment."""


@dataclass(frozen=True)
class CommandResult:
    """A command's outcome, in the shape git.py expects of subprocess."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ExecTarget(ABC):
    """A filesystem plus a way to run commands in it, rooted at a repo."""

    repo: str

    # A target the harness created for this run (a sandbox claim, a container)
    # and so may remove afterwards. Subclasses that create targets override it.
    owned = False

    def release(self) -> str:
        """Remove a target this run created. Returns what was done, for the log."""
        return ""

    def attach_hint(self) -> str:
        """Flags that reattach a later run to this same target."""
        return ""

    @property
    def root(self) -> PurePosixPath:
        """The repo root as pure path math -- never touches the local filesystem."""
        return PurePosixPath(self.repo)

    def path(self, relative: str | PurePosixPath) -> PurePosixPath:
        return self.root / relative

    @abstractmethod
    def run_shell(
        self, command: str, *, check: bool = True, stdin: str | None = None, cwd: str | None = None
    ) -> CommandResult:
        """Run a shell command in the repo, or in `cwd` (for work before the repo exists)."""

    @abstractmethod
    def sandbox(self) -> "Sandbox":
        """The Strands sandbox the agent's tools execute through."""

    @abstractmethod
    def describe(self) -> str:
        """One line naming where work happens, for the system prompt and logs."""

    def run(
        self, argv: list[str], *, check: bool = True, stdin: str | None = None, cwd: str | None = None
    ) -> CommandResult:
        """Run an argv in the repo, quoting it into a shell string."""
        return self.run_shell(shlex.join(argv), check=check, stdin=stdin, cwd=cwd)

    def exists(self) -> bool:
        """True if the repo directory exists in the target."""
        return self.run_shell(f"test -d {shlex.quote(self.repo)}", check=False, cwd="/").returncode == 0

    def missing_commands(self, names: tuple[str, ...]) -> list[str]:
        """Which of `names` are not on the target's PATH."""
        probe = "; ".join(f"command -v {shlex.quote(n)} >/dev/null || echo {shlex.quote(n)}" for n in names)
        return self.run_shell(probe, check=False, cwd="/").stdout.split()

    def _check(self, command: str, result: CommandResult, *, check: bool) -> CommandResult:
        if check and result.returncode != 0:
            raise ExecError(
                f"{command.splitlines()[0][:120]} failed in {self.describe()}: "
                f"{(result.stderr or result.stdout).strip()[:400]}"
            )
        return result

    # --- files, used for the task document ---

    def read_file(self, relative: str) -> str | None:
        """Contents of a repo-relative file, or None if it does not exist."""
        target = self.path(relative)
        proc = self.run_shell(f"cat {shlex.quote(str(target))}", check=False)
        return proc.stdout if proc.returncode == 0 else None

    def is_dir(self, relative: str) -> bool:
        """True if a repo-relative directory exists in the target."""
        quoted = shlex.quote(str(self.path(relative)))
        return self.run_shell(f"test -d {quoted}", check=False).returncode == 0

    def write_file(self, relative: str, content: str) -> None:
        """Write a repo-relative file, creating parent directories."""
        target = self.path(relative)
        self.run_shell(
            f"mkdir -p {shlex.quote(str(target.parent))} && cat > {shlex.quote(str(target))}",
            stdin=content,
        )


@dataclass(frozen=True)
class SubprocessTarget(ExecTarget):
    """A target reached by spawning a local process (sh, docker/podman, kubectl).

    Everything goes through `exec_prefix()` -- the argv that gets a command into
    the target -- so the harness's own commands, the agent's tools, streamed
    runs, and interactive sessions all land in the same place by construction.
    """

    # The agent's environment there, beyond the target's own: what build_cache sets (cli.py). Its tools
    # have it (sandbox()); the harness's own commands do not need it.
    env: tuple[tuple[str, str], ...] = ()

    @abstractmethod
    def exec_prefix(self, *, stdin: bool = False, tty: bool = False) -> list[str]:
        """Argv prefix that runs what follows inside the target. Empty for local."""

    def _wrap(self, command: str, *, stdin: bool = False, tty: bool = False, cwd: str | None = None) -> list[str]:
        return [*self.exec_prefix(stdin=stdin, tty=tty), "sh", "-c", f"cd {shlex.quote(cwd or self.repo)} && {command}"]

    def run_shell(
        self, command: str, *, check: bool = True, stdin: str | None = None, cwd: str | None = None
    ) -> CommandResult:
        argv = self._wrap(command, stdin=stdin is not None, cwd=cwd)
        proc = subprocess.run(argv, capture_output=True, text=True, input=stdin)
        return self._check(command, CommandResult(proc.returncode, proc.stdout, proc.stderr), check=check)

    def stream_shell(self, command: str, on_line: Callable[[str], None], *, cwd: str | None = None) -> int:
        """Run a command, handing each stdout line to `on_line` as it arrives.

        stderr passes straight through to the driver's own stderr.
        """
        with subprocess.Popen(self._wrap(command, cwd=cwd), stdout=subprocess.PIPE, text=True) as proc:
            assert proc.stdout is not None
            for line in proc.stdout:
                on_line(line.rstrip("\n"))
        return proc.returncode

    def interactive_shell(self, command: str, *, cwd: str | None = None) -> int:
        """Run a command attached to this terminal (a TTY is allocated in the target)."""
        return subprocess.call(self._wrap(command, stdin=True, tty=True, cwd=cwd))

    def sandbox(self) -> "Sandbox":
        from .exec_sandbox import CommandSandbox

        return CommandSandbox(self.exec_prefix(), working_dir=self.repo, where=self.describe(),
                              stdin_prefix=self.exec_prefix(stdin=True), env=dict(self.env))


@dataclass(frozen=True)
class LocalTarget(SubprocessTarget):
    """Run on this machine. Bypasses the shell for file access."""

    def exec_prefix(self, *, stdin: bool = False, tty: bool = False) -> list[str]:
        return []

    def sandbox(self) -> "Sandbox":
        from .repo_env import RepoEnvironment

        return RepoEnvironment(Path(self.repo))

    def describe(self) -> str:
        return f"local directory {self.repo}"

    def read_file(self, relative: str) -> str | None:
        path = Path(self.repo) / relative
        return path.read_text() if path.is_file() else None

    def write_file(self, relative: str, content: str) -> None:
        path = Path(self.repo) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def force_remove(binary: str, *containers: str) -> subprocess.CompletedProcess[str]:
    """Remove containers now, running or not: podman with no grace period
    (`-t 0`, which docker does not take; its -f kills at once)."""
    wait = ["-t", "0"] if binary == "podman" else []
    return subprocess.run([binary, "rm", "-f", *wait, *containers], capture_output=True, text=True)


@dataclass(frozen=True)
class DockerTarget(SubprocessTarget):
    """Run inside a container via `docker exec` or `podman exec`."""

    container: str = ""
    binary: str = "docker"
    # True when the harness started this container (see containers.py).
    owned: bool = False
    # With egress=log/allowlist: the gateway sidecar and the internal network,
    # both removed along with the container, and the file the gateway's events
    # are saved to first.
    proxy: str = ""
    network: str = ""
    events: str = ""

    def exec_prefix(self, *, stdin: bool = False, tty: bool = False) -> list[str]:
        argv = [self.binary, "exec"]
        if stdin:
            argv.append("-i")
        if tty:
            argv.append("-t")
        return [*argv, self.container]

    def describe(self) -> str:
        egress = f" (egress via {self.proxy})" if self.proxy else ""
        return f"{self.binary} container {self.container} at {self.repo}{egress}"

    def release(self) -> str:
        if not self.owned:
            return ""
        saved = self.save_events()
        removed = [name for name in (self.container, self.proxy) if name]
        proc = force_remove(self.binary, *removed)
        if proc.returncode != 0:
            return f"Could not remove {', '.join(removed)}: {proc.stderr.strip()}{saved}"
        if self.network:
            subprocess.run([self.binary, "network", "rm", self.network], capture_output=True)
            removed.append(f"network {self.network}")
        return f"Removed {', '.join(removed)}.{saved}"

    def save_events(self) -> str:
        """Copy the gateway's events (its stdout) out before it is removed."""
        if not (self.proxy and self.events):
            return ""
        proc = subprocess.run([self.binary, "logs", self.proxy], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if proc.returncode != 0 or not proc.stdout:
            return ""
        path = Path(self.events)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(proc.stdout)
        return f" Egress events: {path}{refused_summary(proc.stdout)}"

    def attach_hint(self) -> str:
        return f"--target {self.binary} --docker-container {self.container}"


def refused_summary(events: bytes, limit: int = 8) -> str:
    """A line naming the destinations a gateway refused, for the end of a run."""
    refused: dict[str, None] = {}
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("verdict") == "deny" and event.get("kind") != "request" and event.get("host") not in (None, "-"):
            port = event.get("port")
            refused[str(event["host"]) if port in (None, 443) else f"{event['host']}:{port}"] = None
    if not refused:
        return ""
    names = list(refused)
    shown = ", ".join(names[:limit]) + (f", and {len(names) - limit} more" if len(names) > limit else "")
    return f"\nRefused: {shown}. `harness egress allow HOST` permits one from the next run on."


@dataclass(frozen=True)
class KubectlTarget(SubprocessTarget):
    """Run inside a Kubernetes pod via `kubectl exec`."""

    pod: str = ""
    namespace: str | None = None
    container: str | None = None
    context: str | None = None
    kubectl: str = "kubectl"

    def kubectl_base(self) -> list[str]:
        argv = [self.kubectl]
        if self.context:
            argv += ["--context", self.context]
        if self.namespace:
            argv += ["-n", self.namespace]
        return argv

    def exec_prefix(self, *, stdin: bool = False, tty: bool = False) -> list[str]:
        argv = [*self.kubectl_base(), "exec"]
        if stdin:
            argv.append("-i")
        if tty:
            argv.append("-t")
        argv.append(self.pod)
        if self.container:
            argv += ["-c", self.container]
        # `--` ends kubectl's flag parsing so the command is never read as flags.
        return [*argv, "--"]

    def describe(self) -> str:
        where = self.pod + (f"/{self.container}" if self.container else "")
        scope = f" in namespace {self.namespace}" if self.namespace else ""
        return f"Kubernetes pod {where}{scope} at {self.repo}"
