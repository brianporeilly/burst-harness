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

A run whose config names its model (model_id) also waits while the server serves another: a server switched to
some other model for a while (a benchmark of a smaller one) would otherwise answer the run with that. This is
asked whether or not runs wait for idle, from the server's /v1/models, and only as a run starts.
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
    return list(models_for(cfg))


def models_for(cfg: HarnessConfig) -> dict[str, str]:
    """servers_for's servers, each with the model the run asks it for: "" when none is named (the run then
    uses whatever it serves)."""
    try:
        names = agents.callable_profiles(cfg)
        configs = ([cfg] if cfg.engine == "strands" else []) + [agents.as_profile(cfg, name) for name in names]
    except (agents.AgentsError, KeyError):
        configs = [cfg] if cfg.engine == "strands" else []  # the run reports a bad profile itself
    found: dict[str, str] = {}
    for each in configs:
        if each.provider == "llamacpp":
            parts = urlsplit(models.llamacpp_url(each))
            named = each.model_id if each.model_id != HarnessConfig.model_id else ""
            server = f"{parts.scheme}://{parts.netloc}".lower()
            found[server] = found.get(server) or named
    return found


def _name(model: str) -> str:
    """A model's name as the harness shows it: llama.cpp names one by its file's path."""
    return model.rsplit("/", 1)[-1].removesuffix(".gguf")


def served_models(server: str) -> list[str] | None:
    """The models the server has loaded, by its /v1/models; None if it does not say (unreachable, or not an
    OpenAI-style list: the run then finds out for itself). A server that loads models on demand lists the ones
    it could load too, with a status: only those loaded count, as asking for another would unload what it
    serves."""
    try:
        with urllib.request.urlopen(f"{server}/v1/models", timeout=HEALTH_TIMEOUT) as response:
            listed = json.load(response)["data"]
        loaded = []
        for entry in listed:
            status = entry.get("status")
            state = status.get("value") if isinstance(status, dict) else status
            if state in (None, "loaded"):
                loaded += [entry["id"], *(entry.get("aliases") or [])]
        return [_name(str(model)) for model in loaded]
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return None


def busy_slots(server: str) -> int:
    """How many of the server's slots are processing, by its /health; 0 if it does not say (not llama.cpp,
    an older one, or unreachable: the run then finds out for itself)."""
    try:
        with urllib.request.urlopen(f"{server}/health", timeout=HEALTH_TIMEOUT) as response:
            return int(json.load(response).get("slots_processing") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


class ModelServers:
    def __init__(self, *, wait_for_idle: bool = False, busy: Callable[[str], int] | None = None,
                 served: Callable[[str], list[str] | None] | None = None) -> None:
        self.wait_for_idle = wait_for_idle
        self.busy = busy  # None: busy_slots, looked up when used
        self.served = served  # None: served_models, likewise
        self.lock = threading.Lock()
        self.held: dict[str, str] = {}  # server -> who has it ("job NAME", "task SLUG")
        # Called (outside the lock) whenever a server is given back, so what waits can try again.
        self.on_release: list[Callable[[], None]] = []

    def take(self, holder: str, servers: Iterable[str] | dict[str, str]) -> str:
        """Take all of these servers for `holder`: "" if it has them now, else why not (nothing taken). Given
        as models_for's {server: model}, a server serving another model than the one named is not taken."""
        wanted = dict(servers) if isinstance(servers, dict) else {}
        servers = list(dict.fromkeys(servers))
        with self.lock:
            for server in servers:
                if (other := self.held.get(server)) not in (None, holder):
                    return f"{server} is in use by {other}"
            for server in servers:
                if server not in self.held and (model := wanted.get(server)):
                    served = (self.served or served_models)(server)
                    if served is not None and _name(model) not in served:
                        return f"{server} is serving {', '.join(served) or 'no model'}, not {_name(model)}"
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
