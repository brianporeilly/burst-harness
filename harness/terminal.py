"""The three ways a task ends, as plain functions both engines call; and the check before a PR.

The Strands engine exposes them as tools (tools.py); the Claude Code engine
applies them from the outcome file Claude writes (claude_code.py). Either way
the harness, not the model, commits, pushes, and opens the PR.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from . import budget, git, project, repo_commands, review, state
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


def log_heading(ctx: HarnessContext) -> str:
    """This burst's entry in PROGRESS.md's log: when it started, the milestone, and the branch."""
    if not ctx.log_heading:
        current = project.Record(ctx.target).current()
        when = datetime.fromtimestamp(ctx.started, UTC).strftime("%Y-%m-%d %H:%MZ")
        ctx.log_heading = f"{when} — {current.id if current else 'no milestone'} · {ctx.branch}"
    return ctx.log_heading


def record(ctx: HarnessContext, *, done: str = "", next_step: str = "", note: str = "") -> str:
    """Add to the record: in a project, this burst's log entry; otherwise the task document's Done (added to,
    never replaced) and Next step. Returns the file written."""
    if ctx.project:
        project.Record(ctx.target).log(log_heading(ctx), done=done, next_step=next_step, note=note)
        return project.PROGRESS
    doc = state.read(ctx.target, ctx.cfg, ctx.slug)
    more = "\n".join(f"- {line.strip().removeprefix('- ')}" for line in done.strip().splitlines() if line.strip())
    return update_doc(ctx, done=(doc.done.rstrip() + "\n" + more).strip() if more else None,
                      next_step=next_step or None, open_questions=note or None)


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
    if getattr(ctx, "project", False) and not ctx.criteria_refused and (unticked := project.Record(ctx.target).unticked()):
        ctx.criteria_refused = True
        listed = "\n".join(f"- {item.id} {item.text}" for item in unticked)
        raise ValueError(f"No PR yet: the current milestone's Accept items aren't all ticked:\n{listed}\n"
                         "Meet each one and tick_criterion it with the evidence, then finish again. If one can't be "
                         "met in this round, finish again and say why in the body, for each: the PR lists them as "
                         "not met.")
    try:
        check(ctx)
    except CheckFailed as failed:
        if ctx.checks["failed"] <= cfg.max_fixup_attempts:
            raise
        # Told to pause and finishing again anyway: the harness pauses it.
        return pause(ctx, summary=f"Stopped trying to open a PR: the repo's check failed {ctx.checks['failed']} "
                                  "times.", next_step=f"Make `{cfg.check_command.strip()}` pass.") + f"\n{failed}"
    body = review.before_pr(ctx, body)  # review_with: its findings go back once (ReviewGiven), then into the body
    if getattr(ctx, "project", False):
        body = f"{project_summary(ctx)}\n\n---\n\n{body}"
    record(ctx, next_step=next_step or "Awaiting PR review.")
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


RECORD_FILES = frozenset((project.PLAN, project.PROGRESS, project.DECISIONS, project.BACKLOG, project.FEEDBACK))


def changed_files(ctx: HarnessContext) -> list[str]:
    """What this branch changes against the base, committed or not, new files included."""
    base = f"{ctx.cfg.remote}/{ctx.cfg.base_branch}"
    since = git.run(ctx.target, "merge-base", base, "HEAD", check=False).strip() or base
    names = git.run(ctx.target, "diff", "--name-only", since, check=False).splitlines()
    names += git.run(ctx.target, "ls-files", "--others", "--exclude-standard", check=False).splitlines()
    return sorted({name.strip() for name in names if name.strip()})


def project_summary(ctx: HarnessContext) -> str:
    """The top of a project's PR, from its record: the milestone's Accept items met and not, plan changes (first,
    for review), this round's decisions, the checkpoint questions, and whether docs changed."""
    record = project.Record(ctx.target)
    current = record.current()
    if current is None:
        return "(PLAN.md names no current milestone.)"
    base_plan = git.run(ctx.target, "show", f"{ctx.cfg.remote}/{ctx.cfg.base_branch}:{project.PLAN}", check=False)
    found = project.plan_change(base_plan, record.read(project.PLAN) or "") if base_plan else ""
    if found == "roadmap" or not ctx.plan_change:
        ctx.plan_change = found or ctx.plan_change
    held = project.ticks(record.read(project.PROGRESS) or "")
    items = [item for item in current.items if not item.struck]
    met = [item for item in items if held.get(item.id, (0,))[0]]
    out = [f"## {current.id} — {current.title}" + (" (complete)" if len(met) == len(items) else
                                                   f" ({len(met)} of {len(items)} Accept items met)")]
    base = f"{ctx.cfg.remote}/{ctx.cfg.base_branch}"
    before = git.run(ctx.target, "show", f"{base}:{project.DECISIONS}", check=False)
    old = set(project.DECISION.findall(before or ""))
    decisions = [line[3:].strip() for line in (record.read(project.DECISIONS) or "").splitlines()
                 if (found := project.DECISION.match(line)) and found.group(1) not in old]
    changes = [d for d in decisions if "Plan change:" in d]
    if ctx.plan_change and not changes:
        changes = ["PLAN.md changed, with no decision recorded for it: see its diff."]
    if changes:
        out += ["", "### Plan changes: review these" + (" (the roadmap changed: the next round waits for you)"
                                                         if ctx.plan_change == "roadmap" else ""),
                *(f"- {d}" for d in changes)]
    out += ["", "### Met", *(f"- [x] {i.id} {i.text} — {held[i.id][1].split(' — ', 1)[-1]}" for i in met)] if met else []
    if unmet := [i for i in items if i not in met]:
        out += ["", "### Not met (the reasons are below)", *(f"- [ ] {i.id} {i.text}" for i in unmet)]
    if others := [d for d in decisions if d not in changes]:
        out += ["", "### Decisions", *(f"- {d}" for d in others)]
    if current.checkpoint:
        out += ["", "### For you to try", current.checkpoint]
    docs = [name for name in changed_files(ctx) if name not in RECORD_FILES
            and (name.startswith("docs/") or name.lower().startswith("readme") or name.endswith((".md", ".rst")))]
    out += ["", "Docs changed: " + (", ".join(f"`{name}`" for name in docs) if docs else "none")]
    return "\n".join(out)


def pause(ctx: HarnessContext, *, summary: str, next_step: str, questions: str | None = None) -> str:
    """Commit and push without a PR. Raises ValueError if the task doc is over its cap."""
    # Added to what earlier bursts recorded: replacing Done with this burst's summary lost theirs.
    if ctx.project:
        record(ctx, done=summary, next_step=next_step, note=f"Questions: {questions}" if questions else "")
    else:
        record(ctx, done=summary, next_step=next_step, note=questions or "")
    steps = _wrap_up(ctx, f"wip({ctx.slug}): {next_step}")
    ctx.terminal = "paused"
    ctx.said = {"summary": summary, "next_step": next_step, **({"questions": questions} if questions else {})}
    return "\n".join([*steps, "Paused for review. No PR opened."])


def abandon(ctx: HarnessContext, *, reason: str) -> str:
    """Commit and push what exists, recording why. Raises ValueError over the cap."""
    record(ctx, next_step=f"Abandoned: {reason}")
    steps = _wrap_up(ctx, f"wip({ctx.slug}): abandoned - {reason}")
    ctx.terminal = "abandoned"
    ctx.said = {"reason": reason}
    return "\n".join([*steps, f"Abandoned: {reason}"])
