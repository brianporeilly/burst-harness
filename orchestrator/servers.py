"""Model servers of our own, one run at a time (and, if asked, only once they are idle).

A llama.cpp server on one GPU does not take kindly to two clients at once: two long contexts can run it out
of memory. So a task's burst, a question's or a job's run that will call one (its own model, or a profile it
may hand work to, provider llamacpp) takes the server first, and gives it back when it ends. While another run
has it, the run waits: a task stays queued, a scheduled job is tried again on the next tick, and a job started
by hand is refused with the reason.

Only what runs through this orchestrator takes turns. With `wait_for_idle` (--wait-for-idle-servers), a run
also waits while the server says it is busy with someone else's requests (llama-server's /health counts the
slots processing): a benchmark, a run from the command line. That is seen only at the start of a run, so
something that starts later can still overlap it.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from harness import agents, models
from harness.config import HarnessConfig

HEALTH_TIMEOUT = 5


def servers_for(cfg: HarnessConfig) -> list[str]:
    """The llama.cpp servers a run with this config may call, as scheme://host:port: its own model on the
    strands engine (Claude Code is its own), and the profiles it may route to or hand work to."""
    try:
        names = agents.callable_profiles(cfg)
        configs = ([cfg] if cfg.engine == "strands" else []) + [agents.as_profile(cfg, name) for name in names]
    except (agents.AgentsError, KeyError):
        configs = [cfg] if cfg.engine == "strands" else []  # the run reports a bad profile itself
    found = []
    for each in configs:
        if each.provider == "llamacpp":
            parts = urlsplit(models.llamacpp_url(each))
            found.append(f"{parts.scheme}://{parts.netloc}".lower())
    return list(dict.fromkeys(found))


def busy_slots(server: str) -> int:
    """How many of the server's slots are processing, by its /health; 0 if it does not say (not llama.cpp,
    an older one, or unreachable: the run then finds out for itself)."""
    try:
        with urllib.request.urlopen(f"{server}/health", timeout=HEALTH_TIMEOUT) as response:
            return int(json.load(response).get("slots_processing") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


class ModelServers:
    def __init__(self, *, wait_for_idle: bool = False, busy: Callable[[str], int] | None = None) -> None:
        self.wait_for_idle = wait_for_idle
        self.busy = busy  # None: busy_slots, looked up when used
        self.lock = threading.Lock()
        self.held: dict[str, str] = {}  # server -> who has it ("job NAME", "task SLUG")
        # Called (outside the lock) whenever a server is given back, so what waits can try again.
        self.on_release: list[Callable[[], None]] = []

    def take(self, holder: str, servers: Iterable[str]) -> str:
        """Take all of these servers for `holder`: "" if it has them now, else why not (nothing taken)."""
        servers = list(dict.fromkeys(servers))
        with self.lock:
            for server in servers:
                if (other := self.held.get(server)) not in (None, holder):
                    return f"{server} is in use by {other}"
            for server in servers if self.wait_for_idle else ():
                if server not in self.held and (busy := (self.busy or busy_slots)(server)):
                    return f"{server} is busy ({busy} slot{'s' if busy > 1 else ''} processing)"
            for server in servers:
                self.held[server] = holder
        return ""

    def hold(self, holder: str, servers: Iterable[str]) -> None:
        """Mark them taken without asking: a run followed again after a restart, already under way."""
        with self.lock:
            for server in servers:
                self.held[server] = holder

    def give_back(self, holder: str, *, wake: bool = True) -> None:
        """Give back what `holder` has, and call on_release. `wake=False` from a caller that holds a lock a
        waiter would take (a runner starting something): it looks at its own queue itself."""
        with self.lock:
            mine = [server for server, who in self.held.items() if who == holder]
            for server in mine:
                del self.held[server]
        if mine and wake:
            for waiter in self.on_release:
                try:
                    waiter()
                except Exception as err:  # noqa: BLE001 -- one waiter's trouble is not the others'
                    print(f"servers: after {holder} gave back {', '.join(mine)}: {err}", flush=True)

    def status(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.held)
