"""Noticing a loop: the same tool call failing the same way, again and again.

A model that is stuck (small local ones, most often) tends to repeat itself: the same command, the same error,
a few edits apart. Each failed tool call is remembered by what it was (the tool and its input) and how it failed
(its result, with durations and timestamps taken out, so two runs of one failing test match). The calls need not
be one after another for the warning; for the stop they must be with nothing new working in between.

  loop_warn   (3)  the time it happens: the model is told, in that tool result, to change its approach
  loop_stop   (5)  the time it happens since anything new worked: the agent is stopped once its tools have
                   run. A lead's burst pauses for review, everything committed, with the loop as the next step
                   (HARNESS_RESULT's `said.loop`); a delegate's report says so, and its lead goes on; a job stops

"Anything new worked": a call that succeeded and had not succeeded before (another tool, or other input). It
sets every count toward the stop back to 0, not the warning's. A model retrying a service that is down now and
then, between other work, is not stuck; one alternating a failing call with the same successful one (`ls`)
is. Replayed over local-llm-benchmark's runs, counting every repeat stopped 9 of 298 that went on to finish
well, nearly all retrying a service the task had down on purpose.

0 turns either off. A call counts as failed when its tool raised or returned an error, or when a shell's exit
code is not 0. A call that succeeds is never counted, however often it is repeated.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

STOPPED = "harness_loop"  # in request_state: the stop was a loop's (agent._call does not restart it)

WARNING = ("\n[harness] This exact call has now failed the same way {count} times. Repeating it will not help: "
           "change your approach (read the error, look at the code it points to, try something else), or "
           "pause_for_review with what is blocking you.")
WARNING_DELEGATE = ("\n[harness] This exact call has now failed the same way {count} times. Repeating it will not "
                    "help: change your approach, or stop and report what is blocking you.")

# Run to run noise in an error: durations (0.52s, 1.2 seconds, 12ms), clock times and dates, memory addresses.
NOISE = re.compile(r"\d+(\.\d+)?\s?(ms|s|sec|seconds)\b|\d{4}-\d\d-\d\d[T ][\d:.]+Z?|\b\d\d:\d\d:\d\d(\.\d+)?|0x[0-9a-f]+")
EXIT_CODE = re.compile(r'"?exit_code"?\s*[:=]\s*(-?\d+)|exit (?:code|status) (-?\d+)', re.I)


def _text(result: Any) -> str:
    parts = []
    for block in (result or {}).get("content") or []:
        if "text" in block:
            parts.append(str(block["text"]))
        elif "json" in block:
            parts.append(json.dumps(block["json"], sort_keys=True))
    return "\n".join(parts)


def failed(result: Any, exception: Exception | None = None) -> bool:
    if exception is not None or (result or {}).get("status") == "error":
        return True
    found = EXIT_CODE.search(_text(result))
    return bool(found) and int(found.group(1) or found.group(2)) != 0


def _key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class Loops:
    def __init__(self, cfg: Any, *, delegate: bool = False) -> None:
        self.warn, self.stop = cfg.loop_warn, cfg.loop_stop
        self.delegate = delegate
        self.seen: dict[str, int] = {}  # each failure, how often: for the warning
        self.streak: dict[str, int] = {}  # each failure, how often since anything new worked: for the stop
        self.worked: set[str] = set()  # the calls that have succeeded
        self.reason = ""

    def note(self, name: str, tool_input: Any, result: Any, exception: Exception | None = None) -> int:
        """Count a call, if it failed: how many times it has failed this way (0 for one that did not fail)."""
        if not failed(result, exception):
            call = _key(name, tool_input)
            if call not in self.worked:  # something new worked: the model is getting somewhere
                self.worked.add(call)
                self.streak.clear()
            return 0
        how = str(exception) if exception is not None else _text(result)
        key = _key(name, tool_input, NOISE.sub("#", how))
        self.seen[key] = count = self.seen.get(key, 0) + 1
        self.streak[key] = streak = self.streak.get(key, 0) + 1
        if self.stop and streak >= self.stop and not self.reason:
            command = json.dumps(tool_input, default=str)
            self.reason = (f"the same {name} call failed the same way {streak} times with nothing new working "
                           f"in between ({command[:150] + '...' if len(command) > 150 else command})")
        return count

    def watch(self, agent: Any) -> None:
        if not (self.warn or self.stop):
            return
        from strands.hooks import AfterToolCallEvent

        def after_call(event: AfterToolCallEvent) -> None:
            use = event.tool_use or {}
            count = self.note(str(use.get("name")), use.get("input"), event.result, event.exception)
            if self.warn and count == self.warn and isinstance(event.result, dict):
                text = (WARNING_DELEGATE if self.delegate else WARNING).format(count=count)
                event.result = {**event.result, "content": [*(event.result.get("content") or []), {"text": text}]}
            if self.reason:
                event.invocation_state.setdefault("request_state", {})[STOPPED] = True
                event.agent.cancel(after_current_tools=True)

        agent.hooks.add_callback(AfterToolCallEvent, after_call)


NEXT_STEP = ("Stopped in a loop: {reason}. Look at why it keeps failing before carrying on; continuing starts "
             "afresh.")


def pause(ctx: Any) -> bool:
    """Pause a lead's burst stopped in a loop, unless it ended some other way meanwhile. True if it did."""
    loops = ctx.loops
    if loops is None or not loops.reason or ctx.terminal or ctx.waiting:
        return False
    from . import terminal

    print(f"\n! Stopped in a loop: {loops.reason}.", flush=True)
    try:
        print(terminal.pause(ctx, summary=f"Stopped in a loop: {loops.reason}.",
                             next_step=NEXT_STEP.format(reason=loops.reason)))
    except Exception as err:  # noqa: BLE001 -- the run ends either way; the sandbox is kept with the work
        print(f"! Could not pause: {type(err).__name__}: {err}")
    ctx.said = {**ctx.said, "loop": loops.reason}
    return True


def of(ctx: Any) -> Loops:
    """The lead's, made the first time it is asked for."""
    if ctx.loops is None:
        ctx.loops = Loops(ctx.cfg)
    return ctx.loops
