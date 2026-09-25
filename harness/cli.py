"""Command line entry point: start, resume, or chat about one task."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import git, state
from .agent import build_agent
from .config import HarnessConfig
from .exec_target import DockerTarget, ExecTarget, KubectlTarget, LocalTarget
from .tools import HarnessContext

NO_STRANDED_WORK = """You stopped without reaching a terminal state, and the working tree still has uncommitted changes:

{status}

Do not leave work stranded. Either finish_with_pr if it is reviewable, or pause_for_review with a summary and the next step."""

RESUME_PROMPT = """Resume this task. Call read_task_doc first and continue from its Next step. Do not re-read the codebase to rebuild context that the document already records."""

# Approximate per-million-token input/output prices, for the burst report only.
_PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}


def _report(agent: Any, model_id: str) -> str:
    raw = agent.event_loop_metrics.accumulated_usage
    usage = dict(raw) if not isinstance(raw, dict) else raw
    inp = usage.get("inputTokens", 0)
    out = usage.get("outputTokens", 0)
    cache_read = usage.get("cacheReadInputTokens", 0) or 0
    cache_write = usage.get("cacheWriteInputTokens", 0) or 0
    lines = [f"  input {inp:,}  output {out:,}  cache read {cache_read:,}  cache write {cache_write:,}"]
    billable_in = inp + cache_write
    if cache_read:
        share = cache_read / (cache_read + billable_in) * 100 if (cache_read + billable_in) else 0
        lines.append(f"  {share:.0f}% of input served from cache")
    else:
        lines.append(
            "  no cache hits — expected on a first burst. On a resume it usually means "
            "the model or effort changed since the previous burst."
        )
    if price := _PRICES.get(model_id):
        # Cache reads bill at ~0.1x input, writes at ~1.25x.
        cost = (inp * price[0] + cache_read * price[0] * 0.1 + cache_write * price[0] * 1.25 + out * price[1]) / 1e6
        lines.append(f"  approx ${cost:.3f}")
    return "\n".join(lines)


def _run(agent: Any, ctx: HarnessContext, prompt: Any, *, interactive: bool) -> Any:
    """Run the agent, resolving interrupts and refusing to leave work stranded."""
    result = agent(prompt)
    fixups = 0
    while True:
        if result.stop_reason == "interrupt":
            if not interactive:
                for itr in result.interrupts:
                    print(f"\n[agent needs input] {getattr(itr, 'reason', '(no detail)')}")
                print("\nRe-run with `chat` to answer, or `resume --message '...'`.")
                return result
            responses = []
            for itr in result.interrupts:
                print(f"\n[agent asks] {getattr(itr, 'reason', '(no detail)')}")
                responses.append({"interruptResponse": {"interruptId": itr.id, "response": input("answer> ")}})
            result = agent(responses)
            continue
        if ctx.terminal or not ctx.cfg.require_clean_tree:
            return result
        if not git.is_dirty(ctx.target):
            return result
        if fixups >= ctx.cfg.max_fixup_attempts:
            print(
                f"\n! Still uncommitted after {fixups} attempt(s). Left as-is:\n"
                f"{git.dirty_summary(ctx.target)}",
                file=sys.stderr,
            )
            return result
        fixups += 1
        result = agent(NO_STRANDED_WORK.format(status=git.dirty_summary(ctx.target)))


def _build_target(cfg: HarnessConfig, repo_arg: str) -> ExecTarget:
    """Construct the execution target. For `local`, the path is resolved here."""
    kind = cfg.target
    if kind == "local":
        return LocalTarget(repo=str(Path(repo_arg).expanduser().resolve()))
    if kind == "docker":
        if not cfg.docker_container:
            raise SystemExit("--docker-container (or HARNESS_DOCKER_CONTAINER) is required for target=docker.")
        return DockerTarget(repo=repo_arg, container=cfg.docker_container)
    if kind == "sandbox":
        from .agent_sandbox import claim
        return claim(
            repo=repo_arg,
            warmpool=cfg.warmpool,
            claim_name=cfg.claim_name,
            namespace=cfg.namespace or "default",
            connection=cfg.sandbox_connection,
            gateway_name=cfg.gateway_name,
            timeout=cfg.command_timeout,
        )
    if kind == "kubectl":
        if not cfg.pod:
            raise SystemExit("--pod (or HARNESS_POD) is required for target=kubectl.")
        return KubectlTarget(
            repo=repo_arg,
            pod=cfg.pod,
            namespace=cfg.namespace or None,
            container=cfg.container or None,
            context=cfg.kube_context or None,
            kubectl=cfg.kubectl,
        )
    raise SystemExit(f"Unknown target {kind!r}. Use local, docker, kubectl, or sandbox.")


def _prepare(args: argparse.Namespace) -> tuple[ExecTarget, HarnessConfig, str]:
    overrides = {
        "provider": args.provider,
        "model_id": args.model,
        "effort": args.effort,
        "max_tokens": args.max_tokens,
        "base_url": args.base_url,
        "base_branch": args.base_branch,
        "pr_draft": args.draft_pr,
        "auto_push": args.push,
        "offload_max_result_tokens": args.offload_max_tokens,
        "target": args.target,
        "pod": args.pod,
        "namespace": args.namespace,
        "container": args.container,
        "kube_context": args.kube_context,
        "docker_container": args.docker_container,
        "warmpool": args.warmpool,
        "claim_name": args.claim,
        "sandbox_connection": args.sandbox_connection,
    }
    # The repo config file is only readable for a local target; remote repos are
    # configured by env and flags.
    probe = Path(args.repo).expanduser()
    cfg = HarnessConfig.load(probe if probe.is_dir() else Path("/nonexistent"), overrides)
    target = _build_target(cfg, args.repo)

    if not git.is_repo(target):
        raise SystemExit(f"No git repository at {target.describe()}.")
    if not cfg.git_user_name and not cfg.git_user_email and not git.has_identity(target):
        cfg = replace(
            cfg,
            git_user_name="burst-harness",
            git_user_email="burst-harness@users.noreply.github.com",
        )
        print(f"No git identity found; commits will be authored as {cfg.git_user_name}.")
    return target, cfg, cfg.branch_prefix + args.slug


def _finish(agent: Any, ctx: HarnessContext) -> int:
    print("\n" + "-" * 60)
    if ctx.terminal == "pr":
        print(f"Finished: {ctx.pr_url}")
    elif ctx.terminal == "pushed_no_pr":
        print("Committed and pushed, but the PR could not be opened (see above).")
    elif ctx.terminal == "paused":
        print(f"Paused. State: {state.doc_relpath(ctx.cfg, ctx.slug)}")
    elif ctx.terminal == "abandoned":
        print("Abandoned.")
    else:
        print("Ended with no terminal state recorded.")
    print(_report(agent, ctx.cfg.model_id))
    return 0


def _context(args: argparse.Namespace) -> tuple[HarnessContext, Any]:
    target, cfg, branch = _prepare(args)
    ctx = HarnessContext(target=target, cfg=cfg, slug=args.slug, branch=branch)
    return ctx, cfg


def cmd_start(args: argparse.Namespace) -> int:
    ctx, cfg = _context(args)
    if git.is_dirty(ctx.target):
        raise SystemExit(
            "The working tree has uncommitted changes. Commit or stash them first — "
            "the harness will not branch over your work."
        )
    print(f"Working in: {ctx.target.describe()}")
    # Branch setup happens here, deterministically, rather than being asked of the model.
    print(git.start_branch(ctx.target, ctx.branch, base=cfg.base_branch, remote=cfg.remote))
    state.write(ctx.target, cfg, state.merge(state.read(ctx.target, cfg, args.slug), goal=args.task))

    agent, _ = build_agent(ctx)
    _run(agent, ctx, args.task, interactive=False)
    return _finish(agent, ctx)


def _switch(ctx: HarnessContext) -> None:
    if git.current_branch(ctx.target) != ctx.branch:
        git.run(ctx.target, "switch", ctx.branch)
        print(f"Switched to {ctx.branch}.")


def cmd_resume(args: argparse.Namespace) -> int:
    ctx, _ = _context(args)
    print(f"Working in: {ctx.target.describe()}")
    _switch(ctx)
    agent, _ = build_agent(ctx)
    restored = len(agent.messages)
    print(
        f"Restored {restored} message(s) from snapshot."
        if restored
        else "No snapshot; resuming from the task document."
    )
    _run(agent, ctx, args.message or RESUME_PROMPT, interactive=False)
    return _finish(agent, ctx)


def cmd_chat(args: argparse.Namespace) -> int:
    ctx, _ = _context(args)
    print(f"Working in: {ctx.target.describe()}")
    _switch(ctx)
    agent, _ = build_agent(ctx)
    restored = len(agent.messages)
    print(f"Restored {restored} message(s)." if restored else "Fresh session.")
    print("Type a message, or /quit to stop (state is saved after every turn).")
    while not ctx.terminal:
        try:
            line = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in {"/quit", "/exit"}:
            break
        _run(agent, ctx, line, interactive=True)
    return _finish(agent, ctx)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness",
        description=__doc__,
        epilog="Every setting is also a HARNESS_* environment variable; see HarnessConfig.env_var_names().",
    )
    parser.add_argument("--repo", default=".", help="Repo path. For remote targets, the path inside the container or pod.")

    where = parser.add_argument_group("where work happens")
    where.add_argument("--target", choices=["local", "docker", "kubectl", "sandbox"],
                       help="Execution target (default: local).")
    where.add_argument("--pod", help="Pod name, for target=kubectl.")
    where.add_argument("--namespace", help="Pod namespace.")
    where.add_argument("--container", help="Container within the pod.")
    where.add_argument("--kube-context", dest="kube_context", help="kubeconfig context.")
    where.add_argument("--docker-container", dest="docker_container", help="Container, for target=docker.")
    where.add_argument("--warmpool", help="Claim a new agent-sandbox from this SandboxWarmPool.")
    where.add_argument("--claim", help="Attach to an existing SandboxClaim by name.")
    where.add_argument("--sandbox-connection", dest="sandbox_connection",
                       choices=["local-tunnel", "in-cluster", "gateway", "direct", "pod-tunnel"],
                       help="How to reach the sandbox router (default: local-tunnel).")

    model = parser.add_argument_group("model")
    model.add_argument("--provider", help="anthropic (default), openai, bedrock, llamacpp, ollama, gemini, "
                       "an installed plugin, or module:function.")
    model.add_argument("--model", help="Override model_id.")
    model.add_argument("--base-url", dest="base_url", help="Endpoint override.")
    model.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    model.add_argument("--max-tokens", type=int, dest="max_tokens")
    model.add_argument("--offload-max-tokens", type=int, dest="offload_max_tokens",
                       help="Offload tool results larger than this. Set very high to disable.")

    flow = parser.add_argument_group("workflow")
    flow.add_argument("--base-branch", dest="base_branch", help="Branch to base work on.")
    flow.add_argument("--draft-pr", dest="draft_pr", action=argparse.BooleanOptionalAction, default=None,
                      help="Open pull requests as drafts.")
    flow.add_argument("--push", action=argparse.BooleanOptionalAction, default=None,
                      help="Push the branch when finishing (default: on).")

    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="Begin a new task on a fresh branch.")
    start.add_argument("slug")
    start.add_argument("--task", required=True, help="What to do.")
    start.set_defaults(func=cmd_start)

    resume = sub.add_parser("resume", help="Continue a task headlessly.")
    resume.add_argument("slug")
    resume.add_argument("--message", help="Instruction for this burst.")
    resume.set_defaults(func=cmd_resume)

    chat = sub.add_parser("chat", help="Drive a task interactively.")
    chat.add_argument("slug")
    chat.set_defaults(func=cmd_chat)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
