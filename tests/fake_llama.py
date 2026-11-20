"""A stand-in llama.cpp server: /v1/chat/completions, streamed, from a script.

Each turn of the script is what the model says next: {"tool": NAME, "args": {...}}
for a tool call (with "text" too, said before it), {"calls": [{"tool", "args"}, ...]} for several at once,
{"text": "..."}, or {"cut": "..."} for a reply cut off at the
output limit (finish_reason "length"). The turn is picked by how many assistant
messages the request already has, so the conversation drives it; a request
with no tools gets a plain reply (`side`, a string or a turn) and does not advance it. In string
arguments, "{last_tool}" becomes the content of the last tool result -- which
shows that a tool's output made it back to the model. A cut-off reply is
served once: the next request at that turn gets the step after it, as if the
model, told it was cut off, did something else.

A recorded burst can be served again: from_transcript() turns its transcript (HARNESS_RESULT's
`transcript`) into a script per agent, what each one said, in order.

    python -m tests.fake_llama SCRIPT.json [PORT]          # for a live run
    python -m tests.fake_llama TRANSCRIPT.json [PORT]      # the lead's turns from a burst's transcript
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

VERBOSE = False


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return json.dumps(content)


def _fill(value: Any, last_tool: str) -> Any:
    if isinstance(value, str):
        return value.replace("{last_tool}", last_tool)
    if isinstance(value, dict):
        return {k: _fill(v, last_tool) for k, v in value.items()}
    return value


def make_handler(script: list[dict[str, Any]], requests: list[dict[str, Any]],
                 side: str | dict[str, Any] = "Side reply", keep_alive: bool = False) -> type[BaseHTTPRequestHandler]:
    skipped = [0]  # cut-off steps already served, which the turn count does not see

    class Handler(BaseHTTPRequestHandler):
        # keep_alive: HTTP/1.1, each reply sized, the connection left open for the next request, as llama-server
        # does. Otherwise HTTP/1.0, closed after each reply.
        protocol_version = "HTTP/1.1" if keep_alive else "HTTP/1.0"

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if VERBOSE:
                print(f"{self.path} tools={len(body.get('tools') or [])} messages={len(body.get('messages', []))}", flush=True)
            messages = body.get("messages", [])
            turn = sum(1 for m in messages if m.get("role") == "assistant")
            tools = [m for m in messages if m.get("role") == "tool"]
            last_tool = _text(tools[-1].get("content")) if tools else ""
            # A request offering no tools is a side call (opencode titles its sessions this way).
            if body.get("tools"):
                step = script[min(turn + skipped[0], len(script) - 1)]
            else:
                step = side if isinstance(side, dict) else {"text": side}
            if "cut" in step and body.get("tools"):
                skipped[0] += 1
            out: list[bytes] = []

            def send(event: dict[str, Any]) -> None:
                out.append(f"data: {json.dumps(event)}\n\n".encode())

            if "tool" in step or "calls" in step:
                calls = step.get("calls") or [step]
                if step.get("text"):
                    send({"choices": [{"delta": {"content": _fill(step["text"], last_tool)}}]})
                send({"choices": [{"delta": {"tool_calls": [{
                    "index": n, "id": f"call-{turn}-{n}", "type": "function",
                    "function": {"name": call["tool"], "arguments": json.dumps(_fill(call.get("args", {}), last_tool))},
                } for n, call in enumerate(calls)]}}]})
                send({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
            elif "cut" in step:
                send({"choices": [{"delta": {"content": step["cut"]}}]})
                send({"choices": [{"delta": {}, "finish_reason": "length"}]})
            else:
                send({"choices": [{"delta": {"content": _fill(step.get("text", ""), last_tool)}}]})
                send({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            send({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
            out.append(b"data: [DONE]\n\n")
            reply = b"".join(out)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            if keep_alive:
                self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


def from_transcript(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """A burst's transcript as scripts, by agent ("lead", then each delegate's profile): each assistant message
    of a Strands agent's, its tool calls (text beside them is dropped) or else its text. A delegate called more
    than once can't be served by one script, whose turns count from each call's start: that is a ValueError, as
    is a Claude Code lead, whose model the harness does not call."""
    scripts: dict[str, list[dict[str, Any]]] = {}
    for agent in data["agents"]:
        name = agent["name"]
        if agent.get("format") != "strands":
            raise ValueError(f"{name}'s messages are {agent.get('format')}'s; only a Strands agent's can be replayed")
        if name in scripts:
            raise ValueError(f"{name} was called more than once; one script can't serve each call")
        steps = []
        for message in agent["messages"]:
            if message["role"] != "assistant":
                continue
            content = message.get("content") or []
            calls = [{"tool": block["toolUse"]["name"], "args": block["toolUse"].get("input") or {}}
                     for block in content if "toolUse" in block]
            if calls:
                steps.append(calls[0] if len(calls) == 1 else {"calls": calls})
            else:
                steps.append({"text": "".join(block.get("text", "") for block in content)})
        scripts[name] = steps
    return scripts


def serve(script: list[dict[str, Any]], port: int = 0, host: str = "127.0.0.1",
          side: str | dict[str, Any] = "Side reply", keep_alive: bool = False) -> tuple[ThreadingHTTPServer, list]:
    """Start it in a thread. Returns the server and the list each request body is appended to."""
    requests: list[dict[str, Any]] = []
    server = ThreadingHTTPServer((host, port), make_handler(script, requests, side, keep_alive))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests


if __name__ == "__main__":
    VERBOSE = True
    with open(sys.argv[1]) as f:
        steps = json.load(f)
    if isinstance(steps, dict):  # a burst's transcript
        steps = from_transcript(steps)["lead"]
    server, seen = serve(steps, int(sys.argv[2]) if len(sys.argv) > 2 else 0,
                         sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1")
    print(f"fake llama-server on http://{server.server_address[0]}:{server.server_address[1]}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
