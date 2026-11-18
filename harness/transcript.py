"""A burst's transcript, as data: every message of the lead and of each delegate, every tool call and model
call with when it started and how long it took, and the burst's metrics. One JSON file per burst, written to
the driver's state directory beside the session snapshots, and named in HARNESS_RESULT's `transcript`.

The session snapshot is not this: it holds what the context window kept, a delegate's conversation never,
and the Claude Code engine has none. The transcript is recorded as the burst runs, so nothing the window
dropped is missing.

    {"schema": 1, "slug", "branch", "engine", "state", "started" (ISO), "seconds", "metrics", "config",
     "agents": [{"name": "lead" | a delegate's profile, "task": a delegate's brief, "format", "system",
                 "messages": [{"role", "content", "at"}, ...]}],
     "tools":  [{"agent", "id", "name", "at", "seconds", "status"}],
     "models": [{"agent", "at", "seconds", "error"?}]}

`at` is seconds since the burst started. `format` says whose message shape it is: "strands" (Bedrock-style
content blocks: text, toolUse, toolResult, reasoningContent, the session snapshot's) or "claude-code" (the
Anthropic API's: text, tool_use, tool_result, thinking, as Claude Code's stream-json carries them).
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = 1


class Transcript:
    def __init__(self) -> None:
        self.started = time.time()
        self.agents: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.models: list[dict[str, Any]] = []
        self._claude: dict[str, Any] | None = None  # the Claude Code lead's entry
        self._claude_tools: dict[str, dict[str, Any]] = {}

    def _at(self) -> float:
        return round(time.time() - self.started, 2)

    def _entry(self, name: str, fmt: str, task: str | None = None) -> dict[str, Any]:
        entry = {"name": name, **({"task": task} if task is not None else {}), "format": fmt, "messages": []}
        self.agents.append(entry)
        return entry

    def watch(self, agent: Any, name: str = "lead", task: str | None = None) -> None:
        """Record a Strands agent from here on: its messages, tool calls and model calls."""
        from strands.hooks.events import (
            AfterModelCallEvent,
            AfterToolCallEvent,
            BeforeModelCallEvent,
            BeforeToolCallEvent,
            MessageAddedEvent,
        )

        entry = self._entry(name, "strands", task)
        if isinstance(getattr(agent, "system_prompt", None), str):
            entry["system"] = agent.system_prompt
        running: dict[str, dict[str, Any]] = {}
        model: dict[str, Any] = {}

        def added(event: Any) -> None:
            # The message itself, not a copy: the harness may add to its content after (a prompt joining the
            # tool results it follows, agent._call), and the transcript should say what the model was sent.
            entry["messages"].append((self._at(), event.message))

        def tool_started(event: Any) -> None:
            use = event.tool_use or {}
            call = {"agent": name, "id": use.get("toolUseId"), "name": use.get("name"), "at": self._at()}
            running[str(call["id"])] = call

        def tool_done(event: Any) -> None:
            use = event.tool_use or {}
            call = running.pop(str(use.get("toolUseId")), None) or {
                "agent": name, "id": use.get("toolUseId"), "name": use.get("name"), "at": self._at()}
            call["seconds"] = round(self._at() - call["at"], 2)
            call["status"] = (event.result or {}).get("status") if event.exception is None else "error"
            self.tools.append(call)

        def model_started(event: Any) -> None:
            model["at"] = self._at()

        def model_done(event: Any) -> None:
            at = model.pop("at", self._at())
            call = {"agent": name, "at": at, "seconds": round(self._at() - at, 2)}
            if getattr(event, "exception", None) is not None:
                call["error"] = type(event.exception).__name__
            self.models.append(call)

        agent.hooks.add_callback(MessageAddedEvent, added)
        agent.hooks.add_callback(BeforeToolCallEvent, tool_started)
        agent.hooks.add_callback(AfterToolCallEvent, tool_done)
        agent.hooks.add_callback(BeforeModelCallEvent, model_started)
        agent.hooks.add_callback(AfterModelCallEvent, model_done)

    def claude(self, event: dict[str, Any], prompt: str | None = None) -> None:
        """One of Claude Code's stream-json events (claude_code.ClaudeCodeEngine._on_line), or the prompt it
        was given. Its tool calls are timed from the tool_use to its tool_result."""
        if self._claude is None:
            self._claude = self._entry("lead", "claude-code")
        messages = self._claude["messages"]
        if prompt is not None:
            messages.append((self._at(), {"role": "user", "content": [{"type": "text", "text": prompt}]}))
            return
        kind = event.get("type")
        if kind not in ("assistant", "user") or not isinstance(event.get("message"), dict):
            return
        message = {"role": event["message"].get("role") or kind, "content": event["message"].get("content")}
        messages.append((self._at(), message))
        for block in message["content"] if isinstance(message["content"], list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                self._claude_tools[str(block.get("id"))] = {"agent": "lead", "id": block.get("id"),
                                                             "name": block.get("name"), "at": self._at()}
            elif block.get("type") == "tool_result" and (call := self._claude_tools.pop(
                    str(block.get("tool_use_id")), None)):
                call["seconds"] = round(self._at() - call["at"], 2)
                call["status"] = "error" if block.get("is_error") else "success"
                self.tools.append(call)

    def empty(self) -> bool:
        return not any(entry["messages"] for entry in self.agents)

    def data(self, ctx: Any, state: str, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": SCHEMA, "slug": ctx.slug, "branch": ctx.branch, "engine": ctx.cfg.engine, "state": state,
            "started": datetime.fromtimestamp(self.started, UTC).isoformat(timespec="seconds"),
            "seconds": self._at(), "metrics": result.get("metrics"), "config": result.get("config"),
            "agents": [{**entry, "messages": [{**message, "at": at} for at, message in entry["messages"]]}
                       for entry in self.agents],
            "tools": sorted(self.tools, key=lambda call: call["at"]),
            "models": self.models,
        }

    def write(self, ctx: Any, state: str, result: dict[str, Any]) -> Path | None:
        """Write the transcript to the run's state directory, keeping the newest transcripts_kept. None when
        there is nothing to write, or transcripts_kept is 0."""
        kept = ctx.cfg.transcripts_kept
        if kept <= 0 or self.empty():
            return None
        folder = ctx.cfg.resolve_state_dir(ctx.state_key) / "transcripts"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(self.started, UTC).strftime("%Y%m%dT%H%M%SZ")
        path = folder / f"{stamp}-{ctx.slug}.json"
        path.write_text(json.dumps(self.data(ctx, state, result), default=str))
        for old in sorted(folder.glob("*.json"))[:-kept]:
            old.unlink(missing_ok=True)
        return path


def of(ctx: Any) -> Transcript:
    """The burst's transcript, made when first asked for."""
    if ctx.transcript is None:
        ctx.transcript = Transcript()
    return ctx.transcript
