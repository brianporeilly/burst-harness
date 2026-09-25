"""kubernetes-sigs/agent-sandbox backend.

agent-sandbox does not expect you to `kubectl exec`. Each sandbox runs a daemon
(`sandboxd`, port 8888) reached through the Sandbox Router, and the SDK drives it
with `sandbox.commands.run()` and `sandbox.files.*`. That matters because the
project exists to run untrusted agent code: under Kata or gVisor, exec and
port-forward may not be available at all, so going through the runtime API is the
portable choice.

Two ways to use a cluster sandbox from the harness:

* ``SandboxTarget`` (here) -- everything over the runtime API. Works whatever the
  container runtime allows.
* ``KubectlTarget`` with ``Sandbox.get_pod_name()`` -- reuses the plain
  ``kubectl exec`` path. Fewer moving parts, but only if your runtime permits exec.
  ``pod_name_for_claim()`` below returns that name.

Warm pools and the exec-in model fit together: a pooled sandbox is created from a
SandboxTemplate *before* any task exists, so its command is fixed and the task
cannot be baked into it. You claim an idle sandbox and drive it from outside --
which is exactly what this backend does. Note that passing ``env`` at claim time
forces a cold start, defeating the pool.

Requires ``pip install k8s-agent-sandbox``; imported lazily so the rest of the
harness does not depend on it.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from strands.sandbox.posix_shell import PosixShellSandbox, validate_env_keys
from strands.sandbox.types import ExecutionResult, StreamChunk
from strands.types.tools import AgentTool
from strands.vended_tools.file_editor import make_file_editor
from strands.vended_tools.file_editor.file_editor import DEFAULT_FILE_EDITOR_DESCRIPTION
from strands.vended_tools.shell import make_shell
from strands.vended_tools.shell.types import SANDBOX_SHELL_DESCRIPTION

from .exec_target import CommandResult, ExecError, ExecTarget

if TYPE_CHECKING:  # pragma: no cover
    from strands.sandbox.base import Sandbox as StrandsSandbox

# The SDK's own default is 60s, which a test suite or install step will blow past.
DEFAULT_COMMAND_TIMEOUT = 900


class AgentSandboxError(RuntimeError):
    """The SDK is missing, or a sandbox could not be claimed."""


def _sdk() -> Any:
    try:
        import k8s_agent_sandbox
    except ModuleNotFoundError as err:
        raise AgentSandboxError(
            "target=sandbox needs the agent-sandbox client: pip install k8s-agent-sandbox"
        ) from err
    return k8s_agent_sandbox


def _connection(kind: str, **kwargs: Any) -> Any:
    """Build a connection config. `kind` picks how the router is reached."""
    sdk = _sdk()
    configs = {
        "local-tunnel": "SandboxLocalTunnelConnectionConfig",
        "in-cluster": "SandboxInClusterConnectionConfig",
        "gateway": "SandboxGatewayConnectionConfig",
        "direct": "SandboxDirectConnectionConfig",
        "pod-tunnel": "SandboxdPodTunnelConnectionConfig",
    }
    name = configs.get(kind)
    if name is None:
        raise AgentSandboxError(
            f"Unknown sandbox connection {kind!r}. Available: {', '.join(sorted(configs))}"
        )
    return getattr(sdk.models, name)(**{k: v for k, v in kwargs.items() if v})


def claim(
    *,
    repo: str,
    warmpool: str = "",
    claim_name: str = "",
    namespace: str = "default",
    connection: str = "local-tunnel",
    gateway_name: str = "",
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> "SandboxTarget":
    """Claim a sandbox from a warm pool, or attach to an existing claim.

    Exactly one of `warmpool` (claim a new one) or `claim_name` (attach to one
    that already exists) is required. Attaching is what makes a resumed burst
    land in the same sandbox as the burst before it.
    """
    sdk = _sdk()
    if bool(warmpool) == bool(claim_name):
        raise AgentSandboxError("Pass exactly one of warmpool= or claim_name=.")
    client = sdk.SandboxClient(
        connection_config=_connection(connection, gateway_name=gateway_name)
    )
    try:
        if claim_name:
            sandbox = client.get_sandbox(claim_name, namespace=namespace)
            name = claim_name
        else:
            # No env= here on purpose: it forces a cold start and defeats the pool.
            sandbox = client.create_sandbox(warmpool=warmpool, namespace=namespace)
            name = getattr(sandbox, "name", "") or warmpool
    except Exception as err:  # the SDK raises its own hierarchy
        raise AgentSandboxError(f"Could not obtain a sandbox: {err}") from err
    return SandboxTarget(repo=repo, sandbox=sandbox, name=name, timeout=timeout)


def pod_name_for_claim(claim_name: str, *, namespace: str = "default", connection: str = "local-tunnel") -> str:
    """The backing pod of an existing claim, for use with ``KubectlTarget``."""
    sdk = _sdk()
    client = sdk.SandboxClient(connection_config=_connection(connection))
    return client.get_sandbox(claim_name, namespace=namespace).get_pod_name()


@dataclass(frozen=True)
class SandboxTarget(ExecTarget):
    """Run through a sandbox's runtime API rather than a local process."""

    sandbox_obj: Any = field(default=None, repr=False)
    name: str = ""
    timeout: int = DEFAULT_COMMAND_TIMEOUT

    def __init__(self, *, repo: str, sandbox: Any, name: str = "", timeout: int = DEFAULT_COMMAND_TIMEOUT) -> None:
        object.__setattr__(self, "repo", repo)
        object.__setattr__(self, "sandbox_obj", sandbox)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "timeout", timeout)

    def run_shell(self, command: str, *, check: bool = True, stdin: str | None = None) -> CommandResult:
        if stdin is not None:
            # sandboxd's run() takes no stdin; write_file goes through the files API.
            raise ExecError("SandboxTarget cannot pipe stdin into a command.")
        shell = f"cd {shlex.quote(self.repo)} && {command}"
        try:
            result = self.sandbox_obj.commands.run(shell, timeout=self.timeout)
        except Exception as err:
            raise ExecError(f"Command failed in {self.describe()}: {err}") from err
        return self._check(
            command,
            CommandResult(result.exit_code, result.stdout, result.stderr),
            check=check,
        )

    def read_file(self, relative: str) -> str | None:
        try:
            return self.sandbox_obj.files.read(str(self.path(relative))).decode("utf-8", "replace")
        except Exception:
            # The SDK raises rather than returning None for a missing path.
            return None

    def write_file(self, relative: str, content: str) -> None:
        target = self.path(relative)
        # The files API writes one file; the parent still has to exist.
        self.run_shell(f"mkdir -p {shlex.quote(str(target.parent))}")
        self.sandbox_obj.files.write(str(target), content)

    def sandbox(self) -> "StrandsSandbox":
        return SandboxdSandbox(self.sandbox_obj, working_dir=self.repo, timeout=self.timeout)

    def describe(self) -> str:
        return f"agent-sandbox {self.name or '(unnamed)'} at {self.repo}"


class SandboxdSandbox(PosixShellSandbox):
    """Strands sandbox backed by an agent-sandbox runtime.

    ``commands.run()`` is not streaming, so output arrives in one chunk when the
    command finishes rather than as it is produced.
    """

    def __init__(self, sandbox: Any, *, working_dir: str | None = None, timeout: int = DEFAULT_COMMAND_TIMEOUT) -> None:
        self._sandbox = sandbox
        self.working_dir = working_dir
        self._timeout = timeout

    def _shell(self, command: str, *, cwd: str | None, env: dict[str, str] | None) -> str:
        effective = cwd if cwd is not None else self.working_dir
        prefix = ""
        if env:
            validate_env_keys(env)
            prefix = "".join(f"export {k}={shlex.quote(v)}; " for k, v in env.items())
        return f"cd {shlex.quote(effective)} && {prefix}{command}" if effective else f"{prefix}{command}"

    async def execute_streaming(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        shell = self._shell(command, cwd=cwd, env=env)
        # The SDK call is blocking HTTP; keep it off the event loop.
        result = await asyncio.to_thread(
            self._sandbox.commands.run, shell, int(timeout or self._timeout)
        )
        if result.stdout:
            yield StreamChunk(data=result.stdout, stream_type="stdout")
        if result.stderr:
            yield StreamChunk(data=result.stderr, stream_type="stderr")
        yield ExecutionResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            output_files=[],
        )

    def get_tools(self) -> list[AgentTool]:
        cwd = f" Working directory: {self.working_dir}." if self.working_dir else ""
        return [
            make_file_editor(
                sandbox=self,
                name="sandbox_file_editor",
                description=f"{DEFAULT_FILE_EDITOR_DESCRIPTION} Files are in an agent-sandbox pod.",
            ),
            make_shell(
                sandbox=self,
                name="sandbox_shell",
                description=f"{SANDBOX_SHELL_DESCRIPTION} Runs in an agent-sandbox pod.{cwd}",
            ),
        ]
