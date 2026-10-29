"""A stand-in llama.cpp server: /v1/chat/completions, streamed, from a script.

Each turn of the script is what the model says next: {"tool": NAME, "args": {...}}
for a tool call, {"text": "..."}, or {"cut": "..."} for a reply cut off at the
output limit (finish_reason "length"). The turn is picked by how many assistant
messages the request already has, so the conversation drives it; a request
with no tools gets a plain reply and does not advance it. In string
arguments, "{last_tool}" becomes the content of the last tool result -- which
shows that a tool's output made it back to the model. A cut-off reply is
served once: the next request at that turn gets the step after it, as if the
model, told it was cut off, did something else.

    python -m tests.fake_llama SCRIPT.json [PORT]     # for a live run
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


def make_handler(script: list[dict[str, Any]], requests: list[dict[str, Any]]) -> type[BaseHTTPRequestHandler]:
    skipped = [0]  # cut-off steps already served, which the turn count does not see

    class Handler(BaseHTTPRequestHandler):
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
            step = script[min(turn + skipped[0], len(script) - 1)] if body.get("tools") else {"text": "Side reply"}
            if "cut" in step:
                skipped[0] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def send(event: dict[str, Any]) -> None:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())

            if "tool" in step:
                send({"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "id": f"call-{turn}", "type": "function",
                    "function": {"name": step["tool"], "arguments": json.dumps(_fill(step.get("args", {}), last_tool))},
                }]}}]})
                send({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
            elif "cut" in step:
                send({"choices": [{"delta": {"content": step["cut"]}}]})
                send({"choices": [{"delta": {}, "finish_reason": "length"}]})
            else:
                send({"choices": [{"delta": {"content": _fill(step.get("text", ""), last_tool)}}]})
                send({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            send({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


def serve(script: list[dict[str, Any]], port: int = 0, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, list]:
    """Start it in a thread. Returns the server and the list each request body is appended to."""
    requests: list[dict[str, Any]] = []
    server = ThreadingHTTPServer((host, port), make_handler(script, requests))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests


if __name__ == "__main__":
    VERBOSE = True
    with open(sys.argv[1]) as f:
        steps = json.load(f)
    server, seen = serve(steps, int(sys.argv[2]) if len(sys.argv) > 2 else 0,
                         sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1")
    print(f"fake llama-server on http://{server.server_address[0]}:{server.server_address[1]}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
