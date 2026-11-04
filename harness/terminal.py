"""The three ways a task ends, as plain functions both engines call.

The Strands engine exposes them as tools (tools.py); the Claude Code engine
applies them from the outcome file Claude writes (claude_code.py). Either way
the harness, not the model, commits, pushes, and opens the PR.
"""

from __future__ import annotations

from . import git, state
from .context import HarnessContext


def update_doc(ctx: HarnessContext, **updates: str | None) -> str:
    """Replace sections of the task document. Raises ValueError over the size cap."""
    doc = state.merge(state.read(ctx.target, ctx.cfg, ctx.slug), **updates)
    return state.write(ctx.target, ctx.cfg, doc)


def _wrap_up(ctx: HarnessContext, message: str) -> list[str]:
    """Commit everything outstanding and push, so nothing is stranded."""
    cfg = ctx.cfg
    steps = [git.commit_all(ctx.target, message, name=cfg.git_user_name, email=cfg.git_user_email)]
    if cfg.auto_push:
        try:
            steps.append(git.push(ctx.target, ctx.branch, remote=cfg.remote, auth=ctx.auth))
        except git.GitError as err:
            steps.append(f"Push failed (work is committed locally): {err}")
    return steps


def finish(ctx: HarnessContext, *, title: str, body: str, next_step: str | None = None) -> str:
    """Commit, push, and open a PR. Raises ValueError if the task doc is over its cap."""
    cfg = ctx.cfg
    update_doc(ctx, next_step=next_step or "Awaiting PR review.")
    steps = _wrap_up(ctx, title)
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


def pause(ctx: HarnessContext, *, summary: str, next_step: str, questions: str | None = None) -> str:
    """Commit and push without a PR. Raises ValueError if the task doc is over its cap."""
    update_doc(ctx, done=summary, next_step=next_step, open_questions=questions)
    steps = _wrap_up(ctx, f"wip({ctx.slug}): {next_step}")
    ctx.terminal = "paused"
    ctx.said = {"summary": summary, "next_step": next_step, **({"questions": questions} if questions else {})}
    return "\n".join([*steps, "Paused for review. No PR opened."])


def abandon(ctx: HarnessContext, *, reason: str) -> str:
    """Commit and push what exists, recording why. Raises ValueError over the cap."""
    update_doc(ctx, next_step=f"Abandoned: {reason}")
    steps = _wrap_up(ctx, f"wip({ctx.slug}): abandoned - {reason}")
    ctx.terminal = "abandoned"
    ctx.said = {"reason": reason}
    return "\n".join([*steps, f"Abandoned: {reason}"])
