"""The single agent factory.

Strands does not snapshot tools, model config, hooks, or interventions -- only
messages and state. So resume depends on rebuilding the agent identically, which
is why there is exactly one construction path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.session import SnapshotSessionManager
from strands.storage import LocalFileStorage
from strands.types.exceptions import MaxTokensReachedException
from strands.vended_plugins.context_offloader import ContextOffloader
from strands.vended_plugins.skills import AgentSkills
from strands.vended_tools import file_editor, handoff_to_user, shell

from . import mcp
from .exec_target import ExecTarget, LocalTarget
from .guardrails import GitGuardrails
from .models import build_model, retry_strategy
from .tools import HarnessContext, make_tools

if TYPE_CHECKING:
    from .config import HarnessConfig

logger = logging.getLogger(__name__)

# Fixed so a resumed run addresses the same snapshot; the session id is the slug.
AGENT_ID = "harness"

SYSTEM_PROMPT = """You are a coding agent working in one git repository on one task.

Working in: {repo}
Branch: {branch} — already created from {remote}/{base} by the harness. Do not create or switch branches.
Task document: {doc}

How to work:
- Call read_task_doc first. It is the authoritative record of this task and what has already happened. Trust it over re-deriving context by reading the codebase.
- Keep it current with update_task_doc as you go. Done is outcomes, not narrative. Decisions is why. Open questions is what you need a human to settle.
- The document is size-capped. If a write is rejected, summarise rather than append.
- Use handoff_to_user to ask a question mid-task; you will be resumed with the answer.

End every session in exactly one terminal state:
- finish_with_pr — the work is reviewable.
- pause_for_review — the work is mid-flight, not yet usable, or blocked on a decision. It commits everything so nothing is stranded.
- abandon — the task should not continue.

Never leave uncommitted changes behind. If you are unsure which state applies, use pause_for_review."""


# Where the context offloader keeps large tool results when the target is not
# this machine. Strands binds the offloader's storage to the agent's sandbox, so
# its path is one in the target; the driver's state_dir does not exist there.
SANDBOX_OFFLOAD_DIR = "/tmp/burst-harness-offload"


def offload_storage(target: ExecTarget, state_dir: str) -> LocalFileStorage:
    """Storage for ContextOffloader: state_dir for a local target, else a path in the target."""
    return LocalFileStorage(state_dir if isinstance(target, LocalTarget) else SANDBOX_OFFLOAD_DIR)


class ChunkedWindow(SlidingWindowConversationManager):
    """The sliding window, trimmed in chunks rather than a message at a time.

    Upstream trims back to exactly `window_size` before every model call once a
    run is past it, so the oldest message changes on every call. Prompt caching
    is a prefix match, so every call then re-reads the whole conversation:
    llama-server re-processes it, Anthropic bills it as uncached input. Measured
    on long local runs, that was 55-78% of wall time. Cutting back to half the
    window (or half the conversation, when proactive compression fires before
    the window is full) keeps the prefix stable until the next cut. A context
    overflow (`e` set) is left to upstream, which truncates tool results first.
    """

    def reduce_context(self, agent: Agent, e: Exception | None = None, **kwargs: Any) -> None:
        if e is not None or self.window_size == 0:
            return super().reduce_context(agent, e, **kwargs)
        full = self.window_size
        self.window_size = max(min(full, len(agent.messages)) // 2, 2)
        try:
            super().reduce_context(agent, **kwargs)
        finally:
            self.window_size = full
        return None


# How many replies in a row may be cut off at the output limit before the run gives up.
CUT_OFF_RETRIES = 2
CUT_OFF = ("[harness] Your last reply hit the output limit and was discarded, unfinished. Do not reason at "
           "length: take the next step with a tool call, or say less in one reply.")


class OutputLimit(RuntimeError):
    """The model's replies kept being cut off at the output limit."""


def call(agent: Agent, prompt: Any = None) -> Any:
    """`agent(prompt)`, carrying on when a reply is cut off at the output limit.

    Strands raises then, with the partial reply in the history. That reply is
    dropped (a model shown its own cut-off reasoning tends to resume the loop it
    was in), the model is told why, and the run goes on: up to CUT_OFF_RETRIES
    times in a row, the count starting again once a reply is finished. Past
    that, OutputLimit.
    """
    cut, mark = 0, -1
    while True:
        try:
            return agent(prompt)
        except MaxTokensReachedException:
            if agent.messages and agent.messages[-1].get("role") == "assistant":
                agent.messages.pop()
            cut = 1 if len(agent.messages) > mark else cut + 1  # finished replies since the last cut start it again
            if cut > CUT_OFF_RETRIES:
                raise OutputLimit(f"the model's replies hit the output limit {cut} times in a row") from None
            logger.warning("A reply hit the output limit and was discarded; telling the model (%d of %d).",
                           cut, CUT_OFF_RETRIES)
            last = agent.messages[-1] if agent.messages else None
            if last is not None and last.get("role") == "user":
                # In the same user turn (a prompt, or tool results): two user messages in a row are refused by some.
                last["content"] = [*last["content"], {"text": CUT_OFF}]
                prompt = None
            else:
                prompt = CUT_OFF
            mark = len(agent.messages) + (prompt is not None)  # the history as the retry starts


def conversation_manager(cfg: HarnessConfig) -> ChunkedWindow:
    """The one conversation manager, for tasks and jobs alike."""
    return ChunkedWindow(window_size=cfg.window_size, per_turn=True, pin_first=cfg.pin_first,
                         proactive_compression=cfg.proactive_compression)


def build_agent(ctx: HarnessContext) -> tuple[Agent, SnapshotSessionManager]:
    """Construct the agent for a run. Returns the agent and its session manager.

    The session manager is returned because there is no public accessor for it on
    Agent, and the caller needs it for snapshot inspection.
    """
    cfg, target = ctx.cfg, ctx.target
    state_dir = cfg.resolve_state_dir(ctx.state_key)
    state_dir.mkdir(parents=True, exist_ok=True)

    model = build_model(cfg)
    sandbox = target.sandbox()
    # A sandbox that vends its own tools (Docker, kubectl) has them auto-registered
    # by Agent. Passing the generic pair as well would put two shells and two file
    # editors in the schema, doubling their tokens and leaving the model to guess.
    exec_tools = [] if sandbox.get_tools() else [shell, file_editor]

    # One storage backend; the session manager and offloader both resolve from
    # the agent-level storage when not given their own.
    storage = LocalFileStorage(str(state_dir))
    session = SnapshotSessionManager(ctx.slug, storage=storage)

    plugins: list[object] = [
        ContextOffloader(
            storage=offload_storage(target, str(state_dir)),
            max_result_tokens=cfg.offload_max_result_tokens,
            preview_tokens=cfg.offload_preview_tokens,
        )
    ]
    if cfg.skills_dir and target.is_dir(cfg.skills_dir):
        # Optional per-repo knowledge, loaded on demand so only one description
        # line per skill sits in the prompt.
        plugins.append(AgentSkills(str(target.path(cfg.skills_dir))))

    doc_rel = f"{cfg.docs_dir}/{ctx.slug}.md"
    agent = Agent(
        model=model,
        retry_strategy=retry_strategy(cfg),
        agent_id=AGENT_ID,
        session_manager=session,
        storage=storage,
        sandbox=sandbox,
        system_prompt=SYSTEM_PROMPT.format(
            repo=target.describe(),
            branch=ctx.branch,
            remote=cfg.remote,
            base=cfg.base_branch,
            doc=doc_rel,
        ),
        tools=[*exec_tools, handoff_to_user, *make_tools(ctx), *mcp.clients(cfg)],
        plugins=plugins,
        interventions=[GitGuardrails(target, cfg)],
        conversation_manager=conversation_manager(cfg),
    )
    return agent, session
