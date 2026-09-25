"""The single agent factory.

Strands does not snapshot tools, model config, hooks, or interventions -- only
messages and state. So resume depends on rebuilding the agent identically, which
is why there is exactly one construction path.
"""

from __future__ import annotations

from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.session import SnapshotSessionManager
from strands.storage import LocalFileStorage
from strands.vended_plugins.context_offloader import ContextOffloader
from strands.vended_plugins.skills import AgentSkills
from strands.vended_tools import file_editor, handoff_to_user, shell

from .guardrails import GitGuardrails
from .models import build_model
from .tools import HarnessContext, make_tools

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
        tools=[*exec_tools, handoff_to_user, *make_tools(ctx)],
        plugins=plugins,
        interventions=[GitGuardrails(target, cfg)],
        conversation_manager=SlidingWindowConversationManager(
            window_size=cfg.window_size,
            per_turn=True,
            pin_first=cfg.pin_first,
            proactive_compression=cfg.proactive_compression,
        ),
    )
    return agent, session
