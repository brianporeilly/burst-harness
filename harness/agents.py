"""Other agents: named model profiles, a lead that hands work to them, and a router that picks the lead.

A profile is an [agents.NAME] table in a config file (the user's, a --config
file, a job.toml; never a repo's, as it says where requests go):

    [agents.coder]
    provider = "llamacpp"
    base_url = "http://typhon:8080"
    model_id = "tiel-coder"
    context_window_limit = 262144
    use_for = "Writing and changing code, running and fixing tests"

    [agents.reviewer]
    provider = "anthropic"
    model_id = "claude-opus-5-5"
    use_for = "Reviewing a change before it is finished; hard debugging"

It takes the model settings (MODEL_KEYS) and use_for, what it is good at,
which is all a router or a lead knows of it. Two more shape it as a
delegate, whatever brief the lead gives it:

    [agents.reviewer]
    prompt = "Review adversarially: find bugs, missed requirements and claims the work does not back up."
    read_only = true

prompt is added to its instructions. read_only takes its file editor away and
tells it to change nothing; it keeps a shell, so the checkout is compared
before and after, and the lead is told of any file it changed anyway. What it leaves out is the
default, not the top-level setting: a profile is a whole model, so a hosted
one never inherits a local base_url.

Three ways to use them, which combine:

* agent = "coder" (--agent): the run uses that profile instead of the
  top-level model.
* delegate_to = ["coder", "reviewer"] (--delegate-to): the lead, the top-level
  model or the run's profile, gets a delegate tool and hands pieces of work to
  those profiles. Each piece runs as a fresh agent with the same sandbox and
  tools, and only its final report comes back, so the lead's context stays
  small. A profile's own delegate_to, when it has one, applies when it is the
  lead; a delegate never delegates further. engine=strands only.
* router = "triage" (--router): before a new task's first burst (or a job's
  run), that profile reads the task and picks which profile of route_to (all
  but itself, when empty) does it. The choice is kept with the task, so every
  later burst uses the same model. A run that names its model (--provider,
  --model, --base-url, --agent, or a task given a model in the orchestrator) is
  not routed; nor is engine=claude-code, which picks its own model. When the
  router cannot be reached or gives no candidate's name, the top-level model
  does the work.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import HarnessConfig

if TYPE_CHECKING:
    from .context import HarnessContext

logger = logging.getLogger(__name__)

# The settings a profile may set: what build_model reads.
MODEL_KEYS = ("provider", "base_url", "token_command", "token_file", "model_params", "model_timeout",
              "model_retries", "model_id", "max_tokens", "effort", "thinking", "context_window_limit")
PROFILE_KEYS = frozenset((*MODEL_KEYS, "use_for", "delegate_to", "prompt", "read_only"))
# How much of a task the router reads.
ROUTER_TASK_CHARS = 8000

# The router's system prompt and its one message. A small model given the task
# as the bare message tends to do it; the task tagged, with the question after
# it, is read as something to route (from benchmarking LFM3-1.7B and E4B).
ROUTER_SYSTEM = "You route tasks to agents. You never do a task yourself: you only name who should."
ROUTER_ASK = """The agents:
{roster}

<task>
{task}
</task>

Which agent should do the task above? Pick the least capable agent that can still do the whole task well, and save \
the more capable ones for tasks that need them. Go by what each agent is for, not by the order they are listed in.

Answer with the agent's name alone on the first line, then one short line saying why."""


class AgentsError(ValueError):
    """A profile, or a reference to one, is wrong."""


def check(cfg: HarnessConfig) -> None:
    """Every profile well-formed, and every name used one of them. Raises AgentsError."""
    for name, profile in cfg.agents.items():
        if not isinstance(profile, dict):
            raise AgentsError(f"[agents.{name}] must be a table of settings.")
        if unknown := sorted(set(profile) - PROFILE_KEYS):
            raise AgentsError(f"[agents.{name}] may not set: {', '.join(unknown)}. "
                              f"It takes {', '.join(sorted(PROFILE_KEYS))}.")
        for key in ("use_for", "prompt"):
            if not isinstance(profile.get(key, ""), str):
                raise AgentsError(f"[agents.{name}] {key} must be a string.")
        if not isinstance(profile.get("read_only", False), bool):
            raise AgentsError(f"[agents.{name}] read_only must be true or false.")
        as_profile(cfg, name)  # each setting the right type
    uses = [("agent", (cfg.agent,) if cfg.agent else ()), ("delegate_to", cfg.delegate_to),
            ("router", (cfg.router,) if cfg.router else ()), ("route_to", cfg.route_to)]
    uses += [(f"[agents.{name}] delegate_to", tuple(profile.get("delegate_to") or ()))
             for name, profile in cfg.agents.items()]
    for setting, names in uses:
        if missing := [name for name in names if name not in cfg.agents]:
            known = ", ".join(sorted(cfg.agents)) or "none are defined"
            raise AgentsError(f"{setting} names no profile: {', '.join(missing)} ({known}).")
    if cfg.router and not candidates(cfg):
        raise AgentsError(f"router {cfg.router} has nothing to pick from: define other profiles, or route_to.")


def as_profile(cfg: HarnessConfig, name: str) -> HarnessConfig:
    """`cfg` with the profile's model in place of its own, and its delegate_to if it has one."""
    profile = cfg.agents[name]
    defaults = HarnessConfig()
    try:
        cfg = replace(cfg, **{key: getattr(defaults, key) for key in MODEL_KEYS}, router="")
        return cfg._merge({key: value for key, value in profile.items() if key in MODEL_KEYS or key == "delegate_to"},
                          source=f"[agents.{name}]")
    except ValueError as err:
        raise AgentsError(str(err)) from err


def use_for(cfg: HarnessConfig, name: str) -> str:
    return str(cfg.agents[name].get("use_for") or "(no use_for given)")


def role(cfg: HarnessConfig, name: str) -> tuple[str, bool]:
    """What the profile adds to a delegate's instructions, and whether it is read-only."""
    profile = cfg.agents[name]
    return str(profile.get("prompt") or "").strip(), bool(profile.get("read_only"))


def describe(cfg: HarnessConfig, name: str) -> str:
    """The profile's name and model, for what is printed."""
    profile = as_profile(cfg, name)
    return f"{name} ({profile.provider} {profile.model_id})"


def roster(cfg: HarnessConfig, names: tuple[str, ...]) -> str:
    return "\n".join(f"- {name}: {use_for(cfg, name)}" for name in names)


def candidates(cfg: HarnessConfig) -> tuple[str, ...]:
    return cfg.route_to or tuple(name for name in cfg.agents if name != cfg.router)


def pick(reply: str, names: tuple[str, ...]) -> str | None:
    """The candidate a router's reply names: its first line, or else the first name in it."""
    reply = _unthought(reply)
    by_lower = {name.lower(): name for name in names}
    lines = [line for line in reply.splitlines() if line.strip()]
    if lines:
        first = re.sub(r"[`*_\"'.:,]", " ", lines[0]).split()
        if first and first[0].lower() in by_lower:
            return by_lower[first[0].lower()]
    for match in re.finditer(r"[\w-]+(?:\.[\w-]+)*", reply):
        if match.group().lower() in by_lower:
            return by_lower[match.group().lower()]
    return None


def _unthought(reply: str) -> str:
    """`reply` without its reasoning, including reasoning cut off before it closed."""
    return re.sub(r"<think>.*?(</think>|$)", "", reply, flags=re.DOTALL).strip()


def _cut_off_text(router: Any) -> str:
    """The text of a router reply cut off at max_tokens, which Strands leaves in its messages."""
    last = router.messages[-1] if router.messages else {}
    if last.get("role") != "assistant":
        return ""
    return "".join(block.get("text", "") for block in last.get("content", []))


def route(cfg: HarnessConfig, task: str) -> tuple[str | None, str]:
    """The router's pick for `task`, and why: (None, the reason) when it gives none."""
    from strands import Agent
    from strands.types.exceptions import MaxTokensReachedException

    from .models import build_model

    names = candidates(cfg)
    cut_off = False
    try:
        # One try: a router that is down should not hold the work up for minutes of backoff.
        router = Agent(model=build_model(as_profile(cfg, cfg.router)), callback_handler=None, retry_strategy=None,
                       system_prompt=ROUTER_SYSTEM)
        try:
            reply = str(router(ROUTER_ASK.format(roster=roster(cfg, names), task=task[:ROUTER_TASK_CHARS])))
        except MaxTokensReachedException:
            # A reply cut off after it named someone still names them.
            reply, cut_off = _cut_off_text(router), True
    except Exception as err:  # noqa: BLE001 -- a router that is down must not stop the work
        return None, f"the router failed ({type(err).__name__}: {str(err).splitlines()[0] if str(err) else ''})"
    chosen = pick(reply, names)
    if chosen is None:
        if cut_off:
            return None, "the router's reply was cut off at max_tokens before it named a candidate"
        return None, f"the router named no candidate: {reply.strip()[:200]!r}"
    why = " ".join(line.strip() for line in _unthought(reply).splitlines()[1:] if line.strip())[:300]
    return chosen, why + (" (reply cut off at max_tokens)" if cut_off else "")


def lead(cfg: HarnessConfig, task: str | None, *, saved: Path | None = None) -> tuple[HarnessConfig, str]:
    """The settings the run's lead uses, and the profile's name ("" for the top-level model).

    `task` is routed when there is a router and it is a new task (or a job); a
    choice in `saved` (an earlier burst's) wins over routing again.
    """
    if cfg.agent:
        return as_profile(cfg, cfg.agent), cfg.agent
    if saved is not None and saved.is_file():
        try:
            name = str(json.loads(saved.read_text()).get("agent") or "")
        except (OSError, ValueError):
            name = ""
        if name in cfg.agents:
            print(f"Lead: {describe(cfg, name)}, as the router chose for this task.")
            return as_profile(cfg, name), name
        if name:
            print(f"! This task was routed to {name}, which is no longer a profile; using the top-level model.")
            return cfg, ""
    if not cfg.router or not task or cfg.engine != "strands":
        return cfg, ""
    print(f"Routing with {describe(cfg, cfg.router)} among {', '.join(candidates(cfg))}...")
    name, why = route(cfg, task)
    if name is None:
        print(f"! Not routed: {why}. The top-level model ({cfg.provider} {cfg.model_id}) does it.")
        return cfg, ""
    print(f"Routed to {describe(cfg, name)}" + (f": {why}" if why else "."))
    if saved is not None:
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(json.dumps({"agent": name, "why": why}))
    return as_profile(cfg, name), name


def lead_for(ctx: HarnessContext, task: str | None) -> None:
    """Put a task's lead in place in `ctx`, routing a new task (task given) once."""
    saved = ctx.cfg.resolve_state_dir(ctx.state_key) / f"{ctx.slug}.agent.json"
    ctx.cfg, ctx.agent = lead(ctx.cfg, task, saved=saved)


def usage_of(agent: Any) -> dict[str, int]:
    raw = agent.event_loop_metrics.accumulated_usage
    return dict(raw) if not isinstance(raw, dict) else raw
