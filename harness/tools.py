"""The harness's own Strands tools: the task document and the three terminal states."""

from __future__ import annotations

from collections.abc import Callable

from strands import tool
from strands.types.tools import ToolContext

from . import state, terminal
from .context import HarnessContext

# In request_state: a tool stopped the loop (stop_loop). Strands keeps a deferred cancel to itself, so this is
# how the harness's own restarts (agent.py) know to leave it stopped.
STOPPED = "harness_stopped"


def stop_loop(tool_context: ToolContext) -> None:
    """End the run once this batch of tools has run: a terminal state, a job's report."""
    tool_context.invocation_state.setdefault("request_state", {})[STOPPED] = True
    tool_context.agent.cancel(after_current_tools=True)

__all__ = ["HarnessContext", "make_tools"]


def make_tools(ctx: HarnessContext) -> list:
    """Build the task tools bound to one run."""
    target, cfg, slug = ctx.target, ctx.cfg, ctx.slug

    def end(tool_context: ToolContext, step: Callable[[], str]) -> str:
        """A terminal state's step, which ends the run once the task has one.

        Stopping the loop here, rather than leaving it to the model to end its turn, is what ends it for a model
        that calls the tool again and again. A call after the task ended does nothing again.
        """
        if ctx.terminal:
            result = f"The task already ended ({ctx.terminal}); nothing was done again. Stop here."
        else:
            try:
                result = step()
            except ValueError as err:
                return f"Rejected: {err}"
        if ctx.terminal:
            stop_loop(tool_context)
        return result

    @tool
    def read_task_doc() -> str:
        """Read the task document: what this task is, what has been done, and what is open.

        Call this before anything else. It is the authoritative record and is
        cheaper and more reliable than re-deriving context from the codebase.
        """
        relative = state.doc_relpath(cfg, slug)
        text = target.read_file(relative)
        if text is None:
            return f"No task document at {relative} yet. Create one with update_task_doc."
        return text

    @tool
    def update_task_doc(
        goal: str | None = None,
        constraints: str | None = None,
        done: str | None = None,
        decisions: str | None = None,
        open_questions: str | None = None,
        next_step: str | None = None,
    ) -> str:
        """Replace one or more sections of the task document.

        Each argument replaces that whole section, so include everything that
        should remain. The document is size-capped; if a write is rejected,
        summarise rather than append.

        Args:
            goal: What this task is trying to achieve.
            constraints: Limits, requirements, and things to avoid.
            done: Outcomes achieved so far, as a terse list -- not a narrative.
            decisions: Choices made and, briefly, why.
            open_questions: What is still unresolved or needs a human decision.
            next_step: The single next actionable step.
        """
        try:
            path = terminal.update_doc(
                ctx,
                goal=goal,
                constraints=constraints,
                done=done,
                decisions=decisions,
                open_questions=open_questions,
                next_step=next_step,
            )
        except ValueError as err:
            return f"Rejected: {err}"
        return f"Updated {path}."

    @tool(context="tool_context")
    def finish_with_pr(title: str, body: str, tool_context: ToolContext, next_step: str | None = None) -> str:
        """Finish the task by committing, pushing, and opening a pull request.

        Use this only when the work is reviewable. If it is incomplete or you
        need a decision, use pause_for_review instead.

        Args:
            title: Pull request title.
            body: Pull request description: what changed and why.
            next_step: Optional follow-up to record in the task document.
        """
        return end(tool_context, lambda: terminal.finish(ctx, title=title, body=body, next_step=next_step))

    @tool(context="tool_context")
    def pause_for_review(summary: str, next_step: str, tool_context: ToolContext, questions: str | None = None) -> str:
        """Stop cleanly with the work committed but no pull request.

        Use this when the work is mid-flight, not yet usable, or blocked on a
        decision. It commits everything so nothing is left stranded, and records
        where things stand so the next session can pick up without re-reading
        the codebase.

        Args:
            summary: What was accomplished, for the Done section.
            next_step: The single next actionable step.
            questions: Anything you need the user to decide.
        """
        return end(tool_context, lambda: terminal.pause(ctx, summary=summary, next_step=next_step, questions=questions))

    @tool(context="tool_context")
    def abandon(reason: str, tool_context: ToolContext) -> str:
        """Stop and record that this task should not continue.

        Commits whatever exists so nothing is stranded, and writes the reason
        into the task document.

        Args:
            reason: Why the task is being abandoned.
        """
        return end(tool_context, lambda: terminal.abandon(ctx, reason=reason))

    return [read_task_doc, update_task_doc, finish_with_pr, pause_for_review, abandon]
