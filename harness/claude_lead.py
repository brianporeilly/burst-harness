"""engine=claude-code with delegate_to: Claude Code leads, and hands pieces of the
work to Strands profiles.

Claude Code is offered a `delegate` tool on an MCP server that lives in the
driver: the Claude Agent SDK's in-process ("sdk") server, whose calls travel
over Claude Code's own stdin and stdout (``--input-format stream-json``). So
they reach the driver through the same exec (podman, docker, kubectl) that runs
`claude` in the target: nothing listens for them in the sandbox or on the
network, and no new credential goes into the sandbox. The driver runs each
delegate as engine=strands runs one (agent.delegate_function): the profile's
model called from the driver, its tools in the target.

The SDK is installed from its source package (see pyproject.toml): its wheels
carry a Claude Code binary of their own, which the harness never runs, as
`claude` is the target's.

Headless runs only: `chat` attaches `claude` to a terminal, which leaves no room
for the SDK's messages, so a chat has no delegate tool.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
from anyio.abc import Process
from anyio.streams.text import TextReceiveStream
from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient, ProcessError, Transport, create_sdk_mcp_server,
                              tool)

# The driver's MCP server, and its tool as Claude Code names it (for --allowedTools).
SERVER = "harness"
TOOL = f"mcp__{SERVER}__delegate"
# The flags that put Claude Code's input on stdin, as the SDK speaks it.
STREAM_INPUT = ("--input-format", "stream-json")
# How long `claude` has to exit once its input ends, before it is stopped.
CLOSE_WAIT = 30


class TargetTransport(Transport):
    """Claude Code in the target: `argv` (the target's exec of `claude`), its stdin and stdout carrying the SDK's
    messages. Each stdout line is also handed to `on_line`, for the engine's printing and accounting."""

    def __init__(self, argv: list[str], on_line: Callable[[str], None]) -> None:
        self.argv, self.on_line = argv, on_line
        self.process: Process | None = None
        self.returncode: int | None = None

    async def connect(self) -> None:
        # stderr passes straight through, as in the engine's runs without the SDK.
        self.process = await anyio.open_process(self.argv, stderr=None)

    async def write(self, data: str) -> None:
        assert self.process is not None and self.process.stdin is not None
        await self.process.stdin.send(data.encode())

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        assert self.process is not None and self.process.stdout is not None
        pending = ""
        async for chunk in TextReceiveStream(self.process.stdout):
            *lines, pending = (pending + chunk).split("\n")
            for line in lines:
                if (message := self._line(line)) is not None:
                    yield message
        if pending and (message := self._line(pending)) is not None:
            yield message
        self.returncode = await self.process.wait()
        if self.returncode != 0:
            raise ProcessError("claude failed", exit_code=self.returncode)

    def _line(self, line: str) -> dict[str, Any] | None:
        self.on_line(line)
        try:
            message = json.loads(line)
        except ValueError:
            return None
        return message if isinstance(message, dict) else None

    async def end_input(self) -> None:
        if self.process is not None and self.process.stdin is not None:
            await self.process.stdin.aclose()

    async def close(self) -> None:
        if self.process is None:
            return
        await self.end_input()
        with anyio.move_on_after(CLOSE_WAIT):
            self.returncode = await self.process.wait()
            return
        print(f"! claude did not exit {CLOSE_WAIT}s after its input ended; stopping it.", file=sys.stderr)
        self.process.kill()
        with anyio.move_on_after(5):
            self.returncode = await self.process.wait()

    def is_ready(self) -> bool:
        return self.process is not None and self.process.returncode is None


def run(argv: list[str], prompt: str, on_line: Callable[[str], None], delegate: Callable[[str, str], str],
        description: str) -> int:
    """One Claude Code session (argv: the target's exec of `claude` with STREAM_INPUT), given `prompt`, its
    delegate calls answered by `delegate`. Returns claude's exit status. A SIGINT cancels a delegate under way."""
    from .agent import sigint_cancels

    with sigint_cancels():
        return anyio.run(_session, argv, prompt, on_line, delegate, description)


async def _session(argv: list[str], prompt: str, on_line: Callable[[str], None],
                   delegate: Callable[[str, str], str], description: str) -> int:
    @tool("delegate", description, {"to": str, "task": str})
    async def delegate_call(args: dict[str, Any]) -> dict[str, Any]:
        # A delegate is a whole Strands run, synchronous: off the event loop, so the session's messages go on.
        report = await anyio.to_thread.run_sync(delegate, str(args.get("to", "")), str(args.get("task", "")))
        return {"content": [{"type": "text", "text": report}]}

    options = ClaudeAgentOptions(mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=[delegate_call])})
    transport = TargetTransport(argv, on_line)
    try:
        async with ClaudeSDKClient(options, transport=transport) as client:
            await client.query(prompt)
            async for _ in client.receive_response():
                pass  # printed and counted line by line, by on_line
    except Exception as err:  # noqa: BLE001 -- a failed session is a status, as for claude run without the SDK
        if not isinstance(err, ProcessError):  # claude's own exit is told by its status
            print(f"! Claude Code session failed: {type(err).__name__}: {err}", file=sys.stderr)
        return transport.returncode or 1
    return transport.returncode or 0
