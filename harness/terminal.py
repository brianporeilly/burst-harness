"""The three ways a task ends, as plain functions both engines call; and the check before a PR.

The Strands engine exposes them as tools (tools.py); the Claude Code engine
applies them from the outcome file Claude writes (claude_code.py). Either way
the harness, not the model, commits, pushes, and opens the PR.
"""

from __future__ import annotations

import time

from . import budget, git, repo_commands, review, state
from .context import HarnessContext

# How much of a failed check's output goes back to the model: its end, where test runners sum up.
CHECK_OUTPUT_CHARS = repo_commands.OUTPUT_CHARS


def check_note(cfg) -> str:
    """For the lead's system prompt: what finishing will run, so it runs the check itself first."""
    command = cfg.check_command.strip()
    return (f"\n\nBefore a PR is opened, the harness runs this repo's check in the repo: `{command}`. If it fails, "
            "no PR is opened and you get its output to fix. Run it yourself before finishing.") if command else ""


class CheckFailed(ValueError):
    """check_command failed: finish_with_pr is refused, with its output for the model to fix."""


def check(ctx: HarnessContext) -> None:
    """Run check_command in the repo before a PR. Raises CheckFailed with what to do about a failure."""
    cfg = ctx.cfg
    command = cfg.check_command.strip()
    if not command:
        return
    timeout = cfg.check_timeout
    if stop := budget.of(ctx).stop_at():  # not past the burst's time: it pauses then, with the work committed
        left = max(int(stop - time.time()), 60)
        timeout = min(timeout, left) if timeout > 0 else left
    ran = repo_commands.run(ctx.target, command, timeout)
    ctx.checks["run"] = ctx.checks.get("run", 0) + 1
    if ran.code == 0:
        return
    failed = ctx.checks["failed"] = ctx.checks.get("failed", 0) + 1
    output, why = ran.output, ran.why(timeout)
    then = ("Fix what it reports, then finish again." if failed < cfg.max_fixup_attempts else
            f"It has failed {failed} times: don't finish again. Pause for review instead, saying what is still "
            "failing and what you tried.")
    raise CheckFailed(f"No PR: the repo's check (`{command}`) {why}. Your work so far is committed to the branch, but no PR was opened. {then}\n"
                      f"{output or '(no output)'}")


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
            ctx.push_error = str(err)
            steps.append(f"Push failed (work is committed locally): {err}")
    return steps


def finish(ctx: HarnessContext, *, title: str, body: str, next_step: str | None = None) -> str:
    """Check, review, commit, push, and open a PR. Raises ValueError if the task doc is over its cap,
    CheckFailed (one) if check_command fails, ReviewGiven (one) with review_with's findings the first time."""
    cfg = ctx.cfg
    if cfg.check_command.strip() or cfg.review_with:
        # Committed and pushed before the check and the review, which can take long: a sandbox that reaches its
        # deadline meanwhile, or a refusal the lead never gets to fix, strands nothing.
        for step in _wrap_up(ctx, f"wip({ctx.slug}): before the check and review"):
            print(step, flush=True)
    try:
        check(ctx)
    except CheckFailed as failed:
        if ctx.checks["failed"] <= cfg.max_fixup_attempts:
            raise
        # Told to pause and finishing again anyway: the harness pauses it.
        return pause(ctx, summary=f"Stopped trying to open a PR: the repo's check failed {ctx.checks['failed']} "
                                  "times.", next_step=f"Make `{cfg.check_command.strip()}` pass.") + f"\n{failed}"
    body = review.before_pr(ctx, body)  # review_with: its findings go back once (ReviewGiven), then into the body
    update_doc(ctx, next_step=next_step or "Awaiting PR review.")
    steps = _wrap_up(ctx, title)
    if ctx.push_error:
        # Nothing on the remote to open a PR from. The sandbox is kept (cli._close), so a later burst
        # pushes the same commits once the remote accepts them.
        ctx.terminal = "push_failed"
        ctx.said = {"error": ctx.push_error}
        return "\n".join([*steps, "No PR opened: the branch is not on the remote."])
    try:
        if not ctx.repo_slug:
            raise git.GitError(f"the {cfg.remote} remote is not a recognisable host/owner/repo URL")
        url = git.open_pr(
            repo=ctx.repo_slug, head=ctx.branch, title=title, body=body,
            base=cfg.base_branch, draft=cfg.pr_draft, cfg=cfg,
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
