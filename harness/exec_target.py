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

import shlex
import subprocess
from abc import ABC, abstractmethod
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

    @property
    def root(self) -> PurePosixPath:
        """The repo root as pure path math -- never touches the local filesystem."""
        return PurePosixPath(self.repo)

    def path(self, relative: str | PurePosixPath) -> PurePosixPath:
        return self.root / relative

    @abstractmethod
    def run_shell(
        self, command: str, *, check: bool = True, stdin: str | None = None
    ) -> CommandResult:
        """Run a shell command in the repo."""

    @abstractmethod
    def sandbox(self) -> "Sandbox":
        """The Strands sandbox the agent's tools execute through."""

    @abstractmethod
    def describe(self) -> str:
        """One line naming where work happens, for the system prompt and logs."""

    def run(
        self, argv: list[str], *, check: bool = True, stdin: str | None = None
    ) -> CommandResult:
        """Run an argv in the repo, quoting it into a shell string."""
        return self.run_shell(shlex.join(argv), check=check, stdin=stdin)

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
    """A target reached by spawning a local process (sh, docker, kubectl)."""

    @abstractmethod
    def _wrap(self, command: str, *, stdin: bool) -> list[str]:
        """Argv that runs `command` as a shell string inside the target, in the repo."""

    def run_shell(
        self, command: str, *, check: bool = True, stdin: str | None = None
    ) -> CommandResult:
        argv = self._wrap(command, stdin=stdin is not None)
        proc = subprocess.run(argv, capture_output=True, text=True, input=stdin)
        return self._check(command, CommandResult(proc.returncode, proc.stdout, proc.stderr), check=check)


@dataclass(frozen=True)
class LocalTarget(SubprocessTarget):
    """Run on this machine. Bypasses the shell for file access."""

    def _wrap(self, command: str, *, stdin: bool) -> list[str]:
        return ["sh", "-c", f"cd {shlex.quote(self.repo)} && {command}"]

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


@dataclass(frozen=True)
class DockerTarget(SubprocessTarget):
    """Run inside a Docker container via `docker exec`."""

    container: str = ""

    def _wrap(self, command: str, *, stdin: bool) -> list[str]:
        argv = ["docker", "exec", "-w", self.repo]
        if stdin:
            argv.append("-i")
        # `--` so a container named like a flag is still positional.
        return [*argv, "--", self.container, "sh", "-c", command]

    def sandbox(self) -> "Sandbox":
        from strands.sandbox.docker import DockerSandbox

        return DockerSandbox(self.container, working_dir=self.repo)

    def describe(self) -> str:
        return f"Docker container {self.container} at {self.repo}"


@dataclass(frozen=True)
class KubectlTarget(SubprocessTarget):
    """Run inside a Kubernetes pod via `kubectl exec`."""

    pod: str = ""
    namespace: str | None = None
    container: str | None = None
    context: str | None = None
    kubectl: str = "kubectl"

    def _wrap(self, command: str, *, stdin: bool) -> list[str]:
        argv = [self.kubectl, "exec"]
        if self.context:
            argv += ["--context", self.context]
        if self.namespace:
            argv += ["-n", self.namespace]
        if stdin:
            argv.append("-i")
        argv.append(self.pod)
        if self.container:
            argv += ["-c", self.container]
        # kubectl has no -w, so the cd goes into the shell string.
        return [*argv, "--", "sh", "-c", f"cd {shlex.quote(self.repo)} && {command}"]

    def sandbox(self) -> "Sandbox":
        from .kube_sandbox import KubectlSandbox

        return KubectlSandbox(
            self.pod,
            namespace=self.namespace,
            container=self.container,
            context=self.context,
            working_dir=self.repo,
            kubectl=self.kubectl,
        )

    def describe(self) -> str:
        where = self.pod + (f"/{self.container}" if self.container else "")
        scope = f" in namespace {self.namespace}" if self.namespace else ""
        return f"Kubernetes pod {where}{scope} at {self.repo}"
