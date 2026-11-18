"""Limits on one burst: model calls, dollars, minutes. A burst that reaches one is paused, never left stranded.

  max_model_calls     the burst's model calls, its delegates' included
  max_cost_usd        its approximate cost (metrics.cost), its delegates' included. A model whose price is not
                      known counts nothing toward it; a local llama.cpp model costs nothing
  max_burst_minutes   its wall time, from when the run started

0, the default, is no limit. Each is per burst: the next one (a Continue) starts again from nothing, so a task
that pauses at its budget carries on when someone decides it should. They are the driver's settings (config file,
HARNESS_MAX_COST_USD and the like), not the repo's: whoever controls a repo does not choose what its runs spend.

On the Strands engine they are checked after each round of tool calls, before the model is called again, for
the lead and for a delegate while it runs. The one that has run past a limit is stopped there (cancelled once its
tools have run), a delegate's lead after it, and the burst pauses for review: everything committed and pushed,
with the limit it reached as the document's next step and in HARNESS_RESULT's `said` (`budget`). A job stops,
with that as its reason. A model call already under way is not cut short, so a limit can be passed by one
call's worth.

Claude Code is given what is left of max_model_calls and max_cost_usd as its own --max-turns and
--max-budget-usd, and the burst pauses when it stops at one. It has no limit on minutes here.
"""

from __future__ import annotations

import time
import weakref
from typing import Any

from . import metrics

STOPPED = "harness_budget"  # in request_state: the stop was the budget's (agent._call does not restart it)


class Budget:
    def __init__(self, ctx: Any) -> None:
        self.ctx, self.cfg = ctx, ctx.cfg
        self.lead: Any = None
        self.running: weakref.WeakSet[Any] = weakref.WeakSet()  # delegates at work now
        self.models: weakref.WeakKeyDictionary[Any, tuple[str, str]] = weakref.WeakKeyDictionary()
        self.reason = ""

    @property
    def limited(self) -> bool:
        return bool(self.cfg.max_model_calls or self.cfg.max_cost_usd or self.cfg.max_burst_minutes)

    def spent(self) -> tuple[int, float]:
        """Model calls and dollars so far: the lead's, the delegates' that ran, and the one running."""
        calls, dollars = 0, 0.0
        live = [self.lead, *self.running] if self.lead is not None else list(self.running)
        for agent in live:
            found = metrics.of_agent(agent, *self.models.get(agent, ("", "")))
            calls += found["model_calls"] or 0
            dollars += found["cost_usd"] or 0.0
        for used in self.ctx.delegated.values():
            done = used.get("metrics") or {}
            calls += done.get("model_calls") or 0
            dollars += done.get("cost_usd") or 0.0
        return calls, dollars

    def check(self) -> str:
        """The limit reached, said for the document; "" while within them."""
        if self.reason or not self.limited:
            return self.reason
        cfg = self.cfg
        minutes = (time.time() - self.ctx.started) / 60
        if cfg.max_burst_minutes and minutes >= cfg.max_burst_minutes:
            self.reason = f"max_burst_minutes: {minutes:.0f} minutes, the limit is {cfg.max_burst_minutes}"
            return self.reason
        calls, dollars = self.spent()
        if cfg.max_model_calls and calls >= cfg.max_model_calls:
            self.reason = f"max_model_calls: {calls} model calls, the limit is {cfg.max_model_calls}"
        elif cfg.max_cost_usd and dollars >= cfg.max_cost_usd:
            self.reason = f"max_cost_usd: about ${dollars:.2f}, the limit is ${cfg.max_cost_usd:.2f}"
        return self.reason

    def watch(self, agent: Any, cfg: Any, *, lead: bool = False) -> None:
        """Check after each round of this agent's tool calls, and stop it once a limit is reached. `cfg` is the
        agent's own (a delegate's profile), for its price."""
        if not self.limited:
            return
        from strands.hooks import AfterToolsEvent

        from .models import model_name

        self.models[agent] = (model_name(cfg), cfg.provider)
        if lead:
            self.lead = agent
        else:
            self.running.add(agent)

        def after_tools(event: AfterToolsEvent) -> None:
            if self.check():
                event.invocation_state.setdefault("request_state", {})[STOPPED] = True
                event.agent.cancel(after_current_tools=True)

        agent.hooks.add_callback(AfterToolsEvent, after_tools)

    def done(self, agent: Any) -> None:
        """A delegate finished: what it used is in ctx.delegated from now on."""
        self.running.discard(agent)

    def claude_args(self, calls: int, dollars: float) -> list[str]:
        """Claude Code's own limits, for what is left after `calls` turns and `dollars` spent."""
        cfg, args = self.cfg, []
        if cfg.max_model_calls:
            args += ["--max-turns", str(max(cfg.max_model_calls - calls, 1))]
        if cfg.max_cost_usd:
            args += ["--max-budget-usd", f"{max(cfg.max_cost_usd - dollars, 0.01):.2f}"]
        return args


NEXT_STEP = ("Stopped at the burst's budget ({reason}). Continue to carry on from here (the next burst's budget "
             "starts again from nothing), or raise the limit.")


def pause(ctx: Any) -> bool:
    """Pause a burst that reached its budget, unless it ended some other way meanwhile. True if it did."""
    limits = ctx.budget
    if limits is None or not limits.reason or ctx.terminal or ctx.waiting:
        return False
    from . import terminal

    print(f"\n! Stopped at the burst's budget: {limits.reason}.", flush=True)
    try:
        print(terminal.pause(ctx, summary=f"Stopped at the burst's budget ({limits.reason}).",
                             next_step=NEXT_STEP.format(reason=limits.reason)))
    except Exception as err:  # noqa: BLE001 -- the run ends either way; the sandbox is kept with the work
        print(f"! Could not pause: {type(err).__name__}: {err}")
    ctx.said = {**ctx.said, "budget": limits.reason}
    return True


def of(ctx: Any) -> Budget:
    """The run's budget, made the first time it is asked for."""
    if ctx.budget is None:
        ctx.budget = Budget(ctx)
    return ctx.budget
