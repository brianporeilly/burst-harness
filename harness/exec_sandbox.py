"""A Strands sandbox that executes through an argv prefix (`kubectl exec ...`,
`podman exec ...`, `docker exec ...`).

Mirrors the shape of :class:`strands.sandbox.docker.DockerSandbox`: a thin
:class:`PosixShellSandbox` backend where file and code operations are inherited
as shell commands, so only ``execute_streaming`` needs implementing. Unlike
DockerSandbox it is not tied to one binary: the target supplies the prefix, so
the agent's tools and the harness's own commands cannot drift apart.
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


class CommandSandbox(PosixShellSandbox):
    """Execute commands by prefixing them with an exec argv."""

    def __init__(self, prefix: list[str], *, working_dir: str | None = None, where: str = "") -> None:
        """Initialize the sandbox.

        Args:
            prefix: Argv that runs what follows inside the target, ending where
                the command goes (e.g. ``[..., "exec", pod, "--"]``).
            working_dir: Directory commands run in. None uses the target's own.
            where: How to describe the target to the model.
        """
        if not prefix:
            raise ValueError("CommandSandbox needs a non-empty exec prefix.")
        self.prefix = list(prefix)
        self.working_dir = working_dir
        self.where = where

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
        """Execute a command in the target, streaming output."""
        args = [*self.prefix[1:], "sh", "-c", self._shell(command, cwd=cwd, env=env)]
        async for chunk in _stream_process(
            self.prefix[0],
            args,
            timeout=timeout,
            enoent_message=f"{self.prefix[0]} is not installed or not on PATH",
        ):
            yield chunk

    def get_tools(self) -> list[AgentTool]:
        """Sandbox-bound tools, described so the model knows where it is working."""
        cwd = f" Working directory: {self.working_dir}." if self.working_dir else ""
        where = f" Runs in {self.where}." if self.where else ""
        return [
            make_file_editor(
                sandbox=self,
                name="sandbox_file_editor",
                description=f"{DEFAULT_FILE_EDITOR_DESCRIPTION}{where}",
            ),
            make_shell(
                sandbox=self,
                name="sandbox_shell",
                description=f"{SANDBOX_SHELL_DESCRIPTION}{where}{cwd}",
            ),
        ]
