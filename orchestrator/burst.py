"""A burst's own process: `python -m orchestrator.burst OUTPUT -- ARGV...`.

Runs ARGV (a `harness run`, `harness ask`), appends what it prints to OUTPUT
(the task's output file, which the page tails and the orchestrator reads the
result from) and copies it to stdout, and exits with its status. A burst is
started this way whether it is a subprocess of the orchestrator or, on a
cluster, a pod of its own (docs/burst-jobs.md), so its output is written one
way.

Signals: a SIGINT for the process group (Stop, or Ctrl-C) reaches the harness
directly, so it is not passed on again; a second one would cut short the
harness's cleanup. A SIGTERM, which is how Kubernetes stops a pod and which
reaches this process alone, is passed on as the SIGINT the harness acts on.

A burst run as a Kubernetes Job is given no credentials, only where to fetch
them and a token good once (BURST_HANDOFF_URL, BURST_HANDOFF_TOKEN; launch.py):
they are fetched here, before the harness starts, and handed to it alone.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import urllib.request
from typing import BinaryIO

USAGE = "usage: python -m orchestrator.burst OUTPUT -- ARGV..."
# Where a burst run as a Job fetches its credentials, and the token it trades for them.
HANDOFF_URL_ENV, HANDOFF_TOKEN_ENV = "BURST_HANDOFF_URL", "BURST_HANDOFF_TOKEN"


def main(args: list[str]) -> int:
    if len(args) < 3 or args[1] != "--":
        print(USAGE, file=sys.stderr)
        return 2
    output, command = args[0], args[2:]
    with open(output, "ab", buffering=0) as log:
        try:
            env = _environment(dict(os.environ))
        except (OSError, ValueError, KeyError) as err:
            _copy(log, f"! could not fetch the burst's credentials: {err}\n".encode())
            return 1
        try:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env)
        except OSError as err:
            _copy(log, f"! could not start the burst: {err}\n".encode())
            return 127
        # A handler, not SIG_IGN: the harness inherits ignored signals, but handlers end at exec.
        signal.signal(signal.SIGINT, lambda signum, frame: None)
        signal.signal(signal.SIGTERM, lambda signum, frame: child.send_signal(signal.SIGINT))
        assert child.stdout is not None
        while chunk := os.read(child.stdout.fileno(), 65536):
            _copy(log, chunk)
        code = child.wait()
    return code if code >= 0 else 128 - code  # killed by a signal: as a shell reports it


def _environment(env: dict[str, str]) -> dict[str, str]:
    """The harness's environment: this one, with the credentials fetched for it if it has a token to trade."""
    url, token = env.pop(HANDOFF_URL_ENV, ""), env.pop(HANDOFF_TOKEN_ENV, "")
    if not url:
        return env
    request = urllib.request.Request(url, data=b"", method="POST", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        values = json.load(response)["env"]
    return {**env, **{str(k): str(v) for k, v in values.items()}}


def _copy(log: BinaryIO, chunk: bytes) -> None:
    log.write(chunk)
    try:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
    except (OSError, ValueError):
        pass  # nobody reading stdout: the file is what counts


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
