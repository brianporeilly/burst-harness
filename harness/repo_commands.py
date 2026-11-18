"""The repo's own commands, run by the harness rather than the model: setup_command before a burst's model
starts, check_command before a PR (terminal.check). Each runs in the repo, where the agent's shell does."""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from typing import Any

from .exec_target import ExecTarget

# How much of a command's output the model is given: its end, where installers and test runners sum up.
OUTPUT_CHARS = 4000


@dataclass
class Ran:
    command: str
    code: int
    output: str  # stdout then stderr, cut to its last OUTPUT_CHARS
    seconds: float
    timed_out: bool

    def why(self, timeout: int) -> str:
        return f"timed out after {timeout}s" if self.timed_out else f"exited {self.code}"


def run(target: ExecTarget, command: str, timeout: int) -> Ran:
    """Run `command` in the repo, stopped after `timeout` seconds (0: never) where coreutils' timeout exists."""
    timed = timeout > 0 and not target.missing_commands(("timeout",))
    started = time.time()
    result = target.run_shell(f"timeout -k 10 {int(timeout)} sh -c {shlex.quote(command)}" if timed else command,
                              check=False)
    output = (result.stdout + ("\n" if result.stdout and result.stderr else "") + result.stderr).strip()
    if len(output) > OUTPUT_CHARS:
        output = "[...]\n" + output[-OUTPUT_CHARS:]
    return Ran(command, result.returncode, output, round(time.time() - started, 1),
               timed and result.returncode in (124, 137))


SETUP_NOTE = """[harness] Before you started, the harness ran this repo's setup in the repo (`{command}`): it {result} \
in {seconds}s. You don't need to run it again.{then}
<setup-output>
{output}
</setup-output>"""


def setup(ctx: Any) -> str:
    """Run setup_command, if any, before the burst's model starts. Returns the note for its first prompt (""
    for none), and records the run in ctx.setup for HARNESS_RESULT's metrics."""
    cfg = ctx.cfg
    command = cfg.setup_command.strip()
    if not command:
        return ""
    print(f"Setting up: {command}", flush=True)
    ran = run(ctx.target, command, cfg.setup_timeout)
    ctx.setup = {"exit": ran.code, "seconds": ran.seconds, **({"timed_out": True} if ran.timed_out else {})}
    print(f"Setup {ran.why(cfg.setup_timeout)} in {ran.seconds}s.", flush=True)
    then = "" if ran.code == 0 else (" It failed: its output is below. Fix what the task needs of it, or pause "
                                     "for review if it is something you can't fix.")
    return SETUP_NOTE.format(command=command, result=ran.why(cfg.setup_timeout), seconds=ran.seconds, then=then,
                             output=ran.output or "(no output)")
