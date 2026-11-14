"""Run an argv on an SSH host: `python -m harness.ssh_exec [-i] [-t] SSH-ARGV... -- ARGV...`.

ssh gives the remote side a single command line, which the login shell there splits again, so an argv handed
to ssh as it is comes out split wrong (`sh -c "cd /x && ls"` arrives as `sh -c cd /x && ls`). Everything that
runs in a target goes through an argv prefix (exec_target.SubprocessTarget), so this is that prefix for an SSH
host: it quotes the argv into the one line ssh sends. The runner account's shell has to be a POSIX one.

-i passes this process's stdin through; without it ssh reads nothing (-n), so it cannot take what was meant
for the harness. -t allocates a terminal there (-tt).
"""

from __future__ import annotations

import os
import shlex
import sys


def command(argv: list[str]) -> list[str]:
    """The ssh argv that runs what follows `--` on the host named at the end of the ssh argv before it."""
    stdin = tty = False
    while argv and argv[0] in ("-i", "-t"):
        stdin, tty = stdin or argv[0] == "-i", tty or argv[0] == "-t"
        argv = argv[1:]
    if "--" not in argv:
        raise SystemExit("usage: python -m harness.ssh_exec [-i] [-t] SSH-ARGV... -- ARGV...")
    split = argv.index("--")
    ssh, run = argv[:split], argv[split + 1:]
    if len(ssh) < 2 or not run:
        raise SystemExit("harness.ssh_exec needs an ssh argv ending in its destination, and a command.")
    flags = ["-tt"] if tty else ([] if stdin else ["-n"])
    return [ssh[0], *flags, *ssh[1:], shlex.join(run)]


def main() -> None:
    argv = command(sys.argv[1:])
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
