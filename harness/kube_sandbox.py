"""Kubernetes execution backend for Strands, via ``kubectl exec``.

Mirrors the shape of :class:`strands.sandbox.docker.DockerSandbox`: a thin
:class:`PosixShellSandbox` backend where file and code operations are inherited
as shell commands, so only ``execute_streaming`` needs implementing.
"""

from __future__ import annotations

import shlex
from collections.abc import AsyncGenerator
from typing import Any

from strands.sandbox.posix_shell import PosixShellSandbox, validate_env_keys
from strands.sandbox.stream_process import _stream_process
from strands.sandbox.types import ExecutionResult, StreamChunk
from strands.types.tools import AgentTool
from strands.vended_tools.file_editor import make_file_editor
from strands.vended_tools.file_editor.file_editor import DEFAULT_FILE_EDITOR_DESCRIPTION
from strands.vended_tools.shell import make_shell
from strands.vended_tools.shell.types import SANDBOX_SHELL_DESCRIPTION


class KubectlSandbox(PosixShellSandbox):
    """Execute commands in a Kubernetes pod via ``kubectl exec``."""

    def __init__(
        self,
        pod: str,
        *,
        namespace: str | None = None,
        container: str | None = None,
        context: str | None = None,
        working_dir: str | None = None,
        kubectl: str = "kubectl",
    ) -> None:
        """Initialize the sandbox.

        Args:
            pod: Name of a running pod.
            namespace: Namespace of the pod. None uses the kubeconfig's current namespace.
            container: Container within the pod. None uses the pod's default container.
            context: kubeconfig context. None uses the current context.
            working_dir: Directory commands run in. None uses the container's own.
            kubectl: Path to the kubectl binary.
        """
        self.pod = pod
        self.namespace = namespace
        self.container = container
        self.context = context
        self.working_dir = working_dir
        self.kubectl = kubectl

    def exec_args(self, command: str, *, cwd: str | None = None, stdin: bool = False) -> list[str]:
        """Build the ``kubectl exec`` argv for a shell command.

        Unlike ``docker exec``, kubectl has no ``-e`` flag and no ``-w`` flag, so
        the working directory and any environment have to be folded into the shell
        command itself.
        """
        args = ["exec"]
        if self.context:
            args += ["--context", self.context]
        if self.namespace:
            args += ["-n", self.namespace]
        if stdin:
            args.append("-i")
        args.append(self.pod)
        if self.container:
            args += ["-c", self.container]
        # `--` terminates kubectl's flag parsing so the command is never mistaken
        # for flags, matching the reasoning in DockerSandbox.
        args += ["--", "sh", "-c", self._shell(command, cwd=cwd)]
        return args

    def _shell(self, command: str, *, cwd: str | None, env: dict[str, str] | None = None) -> str:
        effective_cwd = cwd if cwd is not None else self.working_dir
        prefix = ""
        if env:
            validate_env_keys(env)
            # Values are interpolated into a shell string here (as the SSH backend
            # does), so each one must be quoted.
            prefix = "".join(f"export {key}={shlex.quote(value)}; " for key, value in env.items())
        if effective_cwd:
            return f"cd {shlex.quote(effective_cwd)} && {prefix}{command}"
        return f"{prefix}{command}"

    async def execute_streaming(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        """Execute a command in the pod, streaming output."""
        args = ["exec"]
        if self.context:
            args += ["--context", self.context]
        if self.namespace:
            args += ["-n", self.namespace]
        args.append(self.pod)
        if self.container:
            args += ["-c", self.container]
        args += ["--", "sh", "-c", self._shell(command, cwd=cwd, env=env)]

        async for chunk in _stream_process(
            self.kubectl,
            args,
            timeout=timeout,
            enoent_message=f"{self.kubectl} is not installed or not on PATH",
        ):
            yield chunk

    def get_tools(self) -> list[AgentTool]:
        """Sandbox-bound tools, described so the model knows where it is working."""
        where = f"pod {self.pod}" + (f"/{self.container}" if self.container else "")
        cwd = f" Working directory: {self.working_dir}." if self.working_dir else ""
        return [
            make_file_editor(
                sandbox=self,
                name="sandbox_file_editor",
                description=f"{DEFAULT_FILE_EDITOR_DESCRIPTION} Files are in Kubernetes {where}.",
            ),
            make_shell(
                sandbox=self,
                name="sandbox_shell",
                description=f"{SANDBOX_SHELL_DESCRIPTION} Runs in Kubernetes {where}.{cwd}",
            ),
        ]
