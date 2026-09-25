"""The harness's own tools: the task document and the three terminal states."""

from __future__ import annotations

from dataclasses import dataclass, field

from strands import tool

from . import git, state
from .config import HarnessConfig
from .exec_target import ExecTarget


@dataclass
class HarnessContext:
    """Per-run state shared between the CLI and the tools."""

    target: ExecTarget
    cfg: HarnessConfig
    slug: str
    branch: str
    # Harness-side snapshot directory name for this repo (see state.state_key).
    state_key: str
    # `git -c` credentials for the harness's own fetch/push; never the agent's.
    auth: git.Auth = ()
    # host/owner/repo for `gh --repo`, or None if the remote isn't recognised.
    repo_slug: str | None = None
    terminal: str | None = None  # "pr" | "pushed_no_pr" | "paused" | "abandoned"
    pr_url: str | None = None
    # Set when a headless run stopped on a question for the user.
    waiting: bool = False
    notes: list[str] = field(default_factory=list)


def make_tools(ctx: HarnessContext) -> list:
    """Build the task tools bound to one run."""
    target, cfg, slug = ctx.target, ctx.cfg, ctx.slug

    def _update(**updates: str | None) -> str:
        doc = state.merge(state.read(target, cfg, slug), **updates)
        return state.write(target, cfg, doc)  # raises ValueError over the cap

    def _wrap_up(message: str) -> list[str]:
        """Commit everything outstanding and push, so nothing is stranded."""
        steps = [git.commit_all(target, message, name=cfg.git_user_name, email=cfg.git_user_email)]
        if cfg.auto_push:
            try:
                steps.append(git.push(target, ctx.branch, remote=cfg.remote, auth=ctx.auth))
            except git.GitError as err:
                steps.append(f"Push failed (work is committed locally): {err}")
        return steps

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
            path = _update(
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

    @tool
    def finish_with_pr(title: str, body: str, next_step: str | None = None) -> str:
        """Finish the task by committing, pushing, and opening a pull request.

        Use this only when the work is reviewable. If it is incomplete or you
        need a decision, use pause_for_review instead.

        Args:
            title: Pull request title.
            body: Pull request description: what changed and why.
            next_step: Optional follow-up to record in the task document.
        """
        try:
            _update(next_step=next_step or "Awaiting PR review.")
        except ValueError as err:
            return f"Rejected: {err}"
        steps = _wrap_up(title)
        try:
            if not ctx.repo_slug:
                raise git.GitError(f"the {cfg.remote} remote is not a recognisable host/owner/repo URL")
            url = git.open_pr(
                repo=ctx.repo_slug, head=ctx.branch, title=title, body=body,
                base=cfg.base_branch, draft=cfg.pr_draft,
            )
        except git.GitError as err:
            # The work is committed and pushed; only the PR step failed. That is
            # still a terminal state, so record it rather than letting the run
            # look like it ended having done nothing.
            ctx.terminal = "pushed_no_pr"
            return "\n".join([*steps, f"Could not open the PR: {err}"])
        ctx.terminal, ctx.pr_url = "pr", url
        kind = "draft PR" if cfg.pr_draft else "PR"
        return "\n".join([*steps, f"Opened {kind}: {url}"])

    @tool
    def pause_for_review(summary: str, next_step: str, questions: str | None = None) -> str:
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
        try:
            _update(done=summary, next_step=next_step, open_questions=questions)
        except ValueError as err:
            return f"Rejected: {err}"
        steps = _wrap_up(f"wip({slug}): {next_step}")
        ctx.terminal = "paused"
        return "\n".join([*steps, "Paused for review. No PR opened."])

    @tool
    def abandon(reason: str) -> str:
        """Stop and record that this task should not continue.

        Commits whatever exists so nothing is stranded, and writes the reason
        into the task document.

        Args:
            reason: Why the task is being abandoned.
        """
        try:
            _update(next_step=f"Abandoned: {reason}")
        except ValueError as err:
            return f"Rejected: {err}"
        steps = _wrap_up(f"wip({slug}): abandoned - {reason}")
        ctx.terminal = "abandoned"
        return "\n".join([*steps, f"Abandoned: {reason}"])

    return [read_task_doc, update_task_doc, finish_with_pr, pause_for_review, abandon]
