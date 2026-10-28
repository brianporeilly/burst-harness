"""Command line entry point: start, resume, run, or chat about one task; list tasks."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import (
    agent_sandbox,
    cluster_session,
    containers,
    egress,
    git,
    job,
    session,
    state,
)
from .agent import build_agent
from .claude_code import ClaudeCodeEngine
from .config import CONFIG_FILENAME, HarnessConfig
from .context import HarnessContext
from .exec_target import ExecError, ExecTarget, KubectlTarget, LocalTarget
from .gateway_control import GatewayError

ENGINES = ("strands", "claude-code")

# Exit codes, so a Job's status means something. Any terminal state -- a PR, a
# pause, an abandon -- is the harness working as designed, hence success.
EXIT_DONE = 0
EXIT_NO_TERMINAL_STATE = 1
EXIT_NEEDS_INPUT = 3

# Where a remote target's checkout lives when --repo is not given.
DEFAULT_REMOTE_REPO = "/workspace/repo"

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
                ctx.waiting = True
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


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    return {
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
        "repo_url": args.repo_url,
        "pod": args.pod,
        "namespace": args.namespace,
        "container": args.container,
        "kube_context": args.kube_context,
        "docker_container": args.docker_container,
        "image": args.image,
        "container_root": args.root,
        "egress": args.egress,
        "gateway_image": args.gateway_image,
        "egress_inject": tuple(w.strip() for w in args.egress_inject.split(",") if w.strip()) if args.egress_inject else None,
        "egress_webhook": args.egress_webhook,
        "egress_allow": tuple(h.strip() for h in args.egress_allow.split(",") if h.strip()) if args.egress_allow else None,
        "engine": args.engine,
        "mcp": tuple(args.mcp) if args.mcp else None,
        "claude_json": args.claude_json,
        "claude_settings": args.claude_settings,
        "claude_permission_mode": args.permission_mode,
        "pool": args.pool,
        "claim_name": args.claim,
        "claim_ttl": args.claim_ttl,
        "keep_claim": args.keep_claim,
    }


def _build_target(cfg: HarnessConfig, repo_arg: str | None, slug: str) -> ExecTarget:
    """Construct the execution target. For `local`, the path is resolved here."""
    kind = cfg.target
    if kind == "local":
        return LocalTarget(repo=str(Path(repo_arg or ".").expanduser().resolve()))
    repo = repo_arg or DEFAULT_REMOTE_REPO
    if kind in ("docker", "podman"):
        if not cfg.docker_container and not cfg.repo_url:
            raise SystemExit(f"target={kind} starts a fresh container: pass --repo-url (or --docker-container to attach).")
        return containers.acquire(cfg, binary=kind, repo=repo, slug=slug)
    if kind == "agent-sandbox":
        if not cfg.repo_url and not cfg.claim_name:
            raise SystemExit("target=agent-sandbox starts from an empty sandbox: pass --repo-url.")
        return agent_sandbox.acquire(cfg, repo=repo, slug=slug, branch=cfg.branch_prefix + slug)
    if kind == "kubectl":
        if not cfg.pod:
            raise SystemExit("--pod (or HARNESS_POD) is required for target=kubectl.")
        return KubectlTarget(
            repo=repo,
            pod=cfg.pod,
            namespace=cfg.namespace or None,
            container=cfg.container or None,
            context=cfg.kube_context or None,
            kubectl=cfg.kubectl,
        )
    raise SystemExit(f"Unknown target {kind!r}. Use local, docker, podman, kubectl, or agent-sandbox.")


def _git_auth(cfg: HarnessConfig, url: str) -> git.Auth:
    """What the harness adds to its own git commands: nothing when the gateway adds the token."""
    return () if "git" in cfg.egress_inject else git.auth_args(url)


def _open(args: argparse.Namespace) -> HarnessContext:
    """Resolve config, acquire the target, and make sure the repo is checked out."""
    overrides = _overrides(args)
    cfg = HarnessConfig.load(None, overrides)
    if cfg.engine not in ENGINES:
        raise SystemExit(f"Unknown engine {cfg.engine!r}. Use {' or '.join(ENGINES)}.")
    repo_toml_read = False
    if cfg.target == "local" and not cfg.repo_url:
        # An existing local checkout: its config file is readable before anything else.
        path = Path(args.repo or ".").expanduser() / CONFIG_FILENAME
        if path.is_file():
            cfg = HarnessConfig.load(path.read_text(), overrides)
        repo_toml_read = True

    if cfg.egress_inject and cfg.target not in ("docker", "podman"):
        raise SystemExit("--egress-inject needs a gateway: --target podman or docker, with --egress log or allowlist.")
    target = _build_target(cfg, args.repo, args.slug)
    try:
        if cfg.repo_url:
            if git.is_repo(target):
                print(f"Using the existing checkout at {target.repo}.")
            else:
                print(git.clone(target, cfg.repo_url, auth=_git_auth(cfg, cfg.repo_url)))
        if not git.is_repo(target):
            raise SystemExit(f"No git repository at {target.describe()}.")
        if not repo_toml_read and (text := target.read_file(CONFIG_FILENAME)):
            cfg = HarnessConfig.load(text, overrides)
        if not cfg.git_user_name and not cfg.git_user_email and not git.has_identity(target):
            cfg = replace(
                cfg,
                git_user_name="burst-harness",
                git_user_email="burst-harness@users.noreply.github.com",
            )
            print(f"No git identity found; commits will be authored as {cfg.git_user_name}.")
        url = git.remote_url(target, cfg.remote)
        return HarnessContext(
            target=target,
            cfg=cfg,
            slug=args.slug,
            branch=cfg.branch_prefix + args.slug,
            state_key=state.state_key(url, target.repo),
            auth=_git_auth(cfg, url),
            repo_slug=git.repo_slug(url),
        )
    except BaseException:
        target.release()
        raise


def _close(ctx: HarnessContext) -> None:
    """Remove a target this run created -- unless that would throw work away."""
    target, cfg = ctx.target, ctx.cfg
    if not target.owned:
        return
    hint = f"Reattach with: harness {target.attach_hint()} ... {ctx.slug}"
    if cfg.keep_claim:
        print(f"Kept {target.describe()} (--keep-claim). {hint}")
        ctx.kept = True
        return
    if ctx.detached:
        print(f"Kept {target.describe()}: a session is still running in it. {hint}")
        ctx.kept = True
        return
    try:
        dirty = git.is_dirty(target)
    except git.GitError:
        dirty = False  # the target is gone or broken; nothing left to save
    if dirty and not ctx.terminal:
        print(f"Kept {target.describe()}: it has uncommitted work. {hint}")
        ctx.kept = True
        return
    print(target.release())


def _finish(report: str | None, ctx: HarnessContext) -> int:
    print("\n" + "-" * 60)
    if ctx.terminal == "pr":
        print(f"Finished: {ctx.pr_url}")
    elif ctx.terminal == "pushed_no_pr":
        print("Committed and pushed, but the PR could not be opened (see above).")
    elif ctx.terminal == "paused":
        print(f"Paused. State: {state.doc_relpath(ctx.cfg, ctx.slug)}")
    elif ctx.terminal == "abandoned":
        print("Abandoned.")
    elif ctx.waiting:
        print("Waiting for an answer from the user.")
    else:
        print("Ended with no terminal state recorded.")
    if report:
        print(report)
    _result_line(ctx, ctx.terminal or ("waiting" if ctx.waiting else "none"))
    if ctx.terminal:
        return EXIT_DONE
    return EXIT_NEEDS_INPUT if ctx.waiting else EXIT_NO_TERMINAL_STATE


def _result_line(ctx: HarnessContext, state_name: str) -> None:
    """One machine-readable line for whatever launched this (a Job, the orchestrator). `target`
    reattaches a later run to this one's sandbox, which is still there if `kept`."""
    print("HARNESS_RESULT " + json.dumps({
        "slug": ctx.slug, "branch": ctx.branch, "state": state_name, "pr": ctx.pr_url,
        "engine": ctx.cfg.engine, "target": ctx.target.attach_hint() or None, "kept": ctx.kept,
    }))


def _session(args: argparse.Namespace, body: Callable[[HarnessContext], str | None]) -> int:
    ctx = _open(args)
    report = None
    try:
        print(f"Working in: {ctx.target.describe()} (engine: {ctx.cfg.engine})")
        report = body(ctx)
    except BaseException:
        _close(ctx)
        # Said even when the run dies, so a launcher knows whether the sandbox was kept.
        _result_line(ctx, ctx.terminal or "error")
        raise
    _close(ctx)
    return _finish(report, ctx)


def _drive(ctx: HarnessContext, prompt: str, *, resume: bool) -> str | None:
    """Run one headless burst with the configured engine. Returns the usage report."""
    if ctx.cfg.engine == "claude-code":
        engine = ClaudeCodeEngine(ctx)
        try:
            engine.prepare()
            engine.headless(prompt)
        finally:
            engine.cleanup()
        return engine.report()
    agent, _ = build_agent(ctx)
    if resume:
        restored = len(agent.messages)
        print(
            f"Restored {restored} message(s) from snapshot."
            if restored
            else "No snapshot; resuming from the task document."
        )
    _run(agent, ctx, prompt, interactive=False)
    return _report(agent, ctx.cfg.model_id)


def _start(ctx: HarnessContext, task: str) -> str | None:
    if git.is_dirty(ctx.target):
        raise SystemExit(
            "The working tree has uncommitted changes. Commit or stash them first — "
            "the harness will not branch over your work."
        )
    # Branch setup happens here, deterministically, rather than being asked of the model.
    print(git.start_branch(ctx.target, ctx.branch, base=ctx.cfg.base_branch, remote=ctx.cfg.remote, auth=ctx.auth))
    state.write(ctx.target, ctx.cfg, state.merge(state.read(ctx.target, ctx.cfg, ctx.slug), goal=task))
    return _drive(ctx, task, resume=False)


def _resume(ctx: HarnessContext, message: str | None) -> str | None:
    print(git.sync_branch(ctx.target, ctx.branch, remote=ctx.cfg.remote, auth=ctx.auth))
    return _drive(ctx, message or RESUME_PROMPT, resume=True)


def _branch_exists(ctx: HarnessContext) -> bool:
    remote = git.remote_has_branch(ctx.target, ctx.branch, remote=ctx.cfg.remote, auth=ctx.auth)
    return remote or bool(git.run(ctx.target, "branch", "--list", ctx.branch))


def cmd_start(args: argparse.Namespace) -> int:
    return _session(args, lambda ctx: _start(ctx, args.task))


def cmd_resume(args: argparse.Namespace) -> int:
    return _session(args, lambda ctx: _resume(ctx, args.message))


def cmd_run(args: argparse.Namespace) -> int:
    """Start the task if its branch does not exist yet, otherwise resume it.

    Idempotent, so a CronJob or a button can fire the same command every time.
    """
    def body(ctx: HarnessContext) -> str | None:
        if _branch_exists(ctx):
            print(f"{ctx.branch} exists; resuming.")
            return _resume(ctx, args.message)
        if not args.task:
            raise SystemExit(f"{ctx.branch} does not exist yet, so --task is needed to start it.")
        return _start(ctx, args.task)

    return _session(args, body)


def cmd_chat(args: argparse.Namespace) -> int:
    def body(ctx: HarnessContext) -> str | None:
        if _branch_exists(ctx):
            print(git.sync_branch(ctx.target, ctx.branch, remote=ctx.cfg.remote, auth=ctx.auth))
        else:
            print(git.start_branch(ctx.target, ctx.branch, base=ctx.cfg.base_branch, remote=ctx.cfg.remote, auth=ctx.auth))
        if ctx.cfg.engine == "claude-code":
            engine = ClaudeCodeEngine(ctx)
            try:
                engine.prepare()
                engine.interactive()
            finally:
                engine.cleanup()
            return None
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
        return _report(agent, ctx.cfg.model_id)

    return _session(args, body)


def cmd_list(args: argparse.Namespace) -> int:
    """Tasks in flight: sandbox claims and harness Jobs, found by their labels."""
    cfg = HarnessConfig.load(None, _overrides(args))
    rows = [("KIND", "NAME", "TASK", "STATUS", "DETAIL")]
    for item in containers.list_containers():
        rows.append((item["binary"], item["name"], item["task"], item["status"], item["image"]))
    try:
        claims = agent_sandbox.list_claims(cfg)
    except agent_sandbox.AgentSandboxError as err:
        claims = []
        print(f"(sandbox claims unavailable: {err})", file=sys.stderr)
    for claim in claims:
        meta, status = claim["metadata"], claim.get("status", {})
        ready = next((c for c in status.get("conditions", []) if c.get("type") == "Ready"), {})
        rows.append((
            "claim",
            meta["name"],
            meta.get("labels", {}).get(agent_sandbox.LABEL_TASK, "?"),
            "ready" if ready.get("status") == "True" else ready.get("reason", "pending"),
            f"until {claim['spec'].get('lifecycle', {}).get('shutdownTime', '?')}  "
            f"{meta.get('annotations', {}).get(agent_sandbox.ANNOTATION_REPO, '')}",
        ))
    for job in agent_sandbox.list_jobs(cfg):
        meta, status = job["metadata"], job.get("status", {})
        phase = "running" if status.get("active") else "succeeded" if status.get("succeeded") else \
            "failed" if status.get("failed") else "pending"
        rows.append((
            "job",
            f"{meta['namespace']}/{meta['name']}",
            meta.get("labels", {}).get(agent_sandbox.LABEL_TASK, "?"),
            phase,
            f"started {status.get('startTime', '-')}",
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)) + "  " + row[4])
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    """Long-lived interactive sandboxes: see session.py."""
    cfg = HarnessConfig.load(None, _overrides(args))
    if cfg.target not in ("local", "podman", "agent-sandbox"):
        raise SystemExit(f"harness session runs on podman or agent-sandbox (target is {cfg.target}).")
    # On a cluster: the same commands, on a claimed sandbox behind the shared gateway.
    sessions: Any = cluster_session if cfg.target == "agent-sandbox" else session
    try:
        if args.action == "list":
            rows = sessions.list_sessions(cfg)
            if not rows:
                print("No sessions.")
            for row in rows:
                print(f"{row['name']:<24} {row['status']:<8} {row['agent']:<12} {row['ui']:<34} {row['repo']}")
            return 0
        if args.action == "reap":
            if sessions is not cluster_session:
                raise SystemExit("harness session reap is for sessions on a cluster: on podman, --idle stops a "
                                 "session by itself.")
            print(cluster_session.reap(cfg))
            return 0
        if not args.name:
            raise SystemExit(f"harness session {args.action} needs a session name.")
        if args.action == "start":
            print(sessions.start(cfg, args.name, port=args.port or 0, bind=args.bind or "", config_file=args.opencode_config or "",
                                terminal=args.terminal, terminal_port=args.terminal_port or 0,
                                idle=args.idle, lifetime=args.lifetime, agent=args.agent,
                                allow=tuple(h.strip() for h in args.allow.split(",") if h.strip())
                                if args.allow is not None else None))
        elif args.action == "stop":
            print(sessions.stop(cfg, args.name))
        else:
            print(sessions.remove(cfg, args.name))
    except (session.SessionError, containers.ContainerError, git.GitError, ExecError, agent_sandbox.AgentSandboxError,
            GatewayError) as err:
        raise SystemExit(str(err)) from err
    return 0


def cmd_egress(args: argparse.Namespace) -> int:
    """What gateways refused, and approving it: see egress.py."""
    cfg = HarnessConfig.load(None, _overrides(args))
    try:
        if args.action == "pending":
            rows = egress.pending()
            if not rows:
                print("Nothing refused and unapproved on a running gateway.")
            for row in rows:
                print(f"{row['id']:<12} {row['destination']:<36} x{row['times']:<4} {row['last']}  {row['sandbox']}")
            if rows:
                print("\nApprove one with: harness egress approve ID [--scope once|session|2h|permanent]")
        elif args.action == "list":
            hosts = containers.approved(cfg)
            print("\n".join(hosts) if hosts else "Nothing is allowed for good beyond egress_allow.")
        elif not args.what:
            raise SystemExit(f"harness egress {args.action} needs {'an id' if args.action == 'approve' else 'a host'}.")
        elif args.action == "approve":
            print(egress.approve(cfg, args.what, args.scope))
        elif args.action == "allow":
            print(egress.allow(cfg, args.what))
        else:
            print(egress.revoke(cfg, args.what))
    except (egress.EgressError, containers.ContainerError) as err:
        raise SystemExit(str(err)) from err
    return 0


def cmd_job(args: argparse.Namespace) -> int:
    """Scheduled or triggered runs that end in a report: see job.py."""
    try:
        spec = job.load(args.dir)
        cfg = job.config(spec, _overrides(args))
        if args.action == "check":
            print(job.describe(spec, cfg))
            job.check(spec)
            return 0
        if args.action == "runs":
            runs = job.list_runs(cfg, spec.name)
            for item in runs:
                refused = (item.get("egress") or {}).get("refused_count")
                print(f"{item['id']}  {item.get('state', '?'):8}  {item.get('seconds', '-'):>5}s  "
                      f"{item.get('engine', '')}"
                      + (f"  {refused} refused" if refused else "")
                      + (f"  {item['reason']}" if item.get("reason") else ""))
            if not runs:
                print(f"No runs of {spec.name} yet.")
            return 0
        return job.run(spec, cfg)
    except (job.JobError, containers.ContainerError) as err:
        raise SystemExit(str(err)) from err


def cmd_release(args: argparse.Namespace) -> int:
    """Remove a target a run kept: a sandbox claim, or a container with its egress sidecar."""
    cfg = HarnessConfig.load(None, _overrides(args))
    if cfg.claim_name:
        target: ExecTarget = agent_sandbox.AgentSandboxTarget(
            repo=DEFAULT_REMOTE_REPO, pod="", claim=cfg.claim_name, owned=True,
            namespace=cfg.namespace or None, context=cfg.kube_context or None, kubectl=cfg.kubectl,
        )
    elif cfg.docker_container:
        target = containers.kept(cfg, binary=cfg.target if cfg.target in ("docker", "podman") else "podman")
    else:
        raise SystemExit("Name what to release: --claim NAME, or --docker-container NAME (with --target docker/podman).")
    print(target.release())
    return 0


def main(argv: list[str] | None = None) -> int:
    # Progress should reach whatever is reading as it happens, even through a pipe.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(
        prog="harness",
        description=__doc__,
        epilog="Every setting is also a HARNESS_* environment variable; see HarnessConfig.env_var_names().",
    )
    parser.add_argument("--repo", help=f"Repo path. Local default: the current directory. "
                                       f"For remote targets, the path inside the container or pod "
                                       f"(default {DEFAULT_REMOTE_REPO}).")
    parser.add_argument("--repo-url", dest="repo_url",
                        help="Clone this into the target first. Required for agent-sandbox.")

    where = parser.add_argument_group("where work happens")
    where.add_argument("--target", choices=["local", "docker", "podman", "kubectl", "agent-sandbox"],
                       help="Execution target (default: local).")
    where.add_argument("--pod", help="Pod name, for target=kubectl.")
    where.add_argument("--namespace", help="Namespace of the pod, or of the sandbox claims.")
    where.add_argument("--container", help="Container within the pod.")
    where.add_argument("--kube-context", dest="kube_context", help="kubeconfig context.")
    where.add_argument("--docker-container", dest="docker_container",
                       help="Attach to this container, for target=docker/podman. Default: start a fresh one.")
    where.add_argument("--image", help="Image a fresh docker/podman container starts from.")
    where.add_argument("--root", action=argparse.BooleanOptionalAction, default=None,
                       help="Run a fresh container as root, so the agent can install packages. Rootless podman "
                            "only, where that is still your own user outside the container; for a session on a "
                            "cluster, claimed from root_pool (a user-namespace template).")
    where.add_argument("--egress", choices=["open", "log", "allowlist"],
                       help="Network for a fresh docker/podman container. log: no route out except a gateway "
                            "that records everything. allowlist: the gateway only reaches --egress-allow hosts, "
                            "over HTTPS (default: open).")
    where.add_argument("--egress-inject", dest="egress_inject",
                       help="Comma-separated credentials the gateway adds so the container never holds them. "
                            "git: the driver's git token, on requests to the repo. model: Claude Code's API "
                            "key or token (engine=claude-code). Needs --egress log or allowlist.")
    where.add_argument("--egress-webhook", dest="egress_webhook",
                       help="URL the gateway POSTs each refusal to, as JSON (--egress allowlist).")
    where.add_argument("--gateway-image", dest="gateway_image",
                       help="Image of the gateway sidecar, for --egress log/allowlist.")
    where.add_argument("--egress-allow", dest="egress_allow",
                       help="Comma-separated hosts for --egress allowlist; replaces the default list. "
                            "'*.example.com' matches subdomains. The repo's host is always added.")
    where.add_argument("--pool", help="SandboxWarmPool to claim a sandbox from, for target=agent-sandbox.")
    where.add_argument("--claim", help="Attach to an existing SandboxClaim instead of claiming a new one.")
    where.add_argument("--claim-ttl", type=int, dest="claim_ttl",
                       help="Seconds until the controller deletes the claim regardless (default 4h).")
    where.add_argument("--keep-claim", dest="keep_claim", action=argparse.BooleanOptionalAction, default=None,
                       help="Keep the claim after the run instead of releasing it.")

    engine = parser.add_argument_group("engine")
    engine.add_argument("--engine", choices=list(ENGINES),
                        help="strands (default): the harness's own agent. claude-code: Claude Code in the target.")
    engine.add_argument("--mcp", action="append", metavar="NAME=URL",
                        help="An MCP server the agent connects to, over HTTPS (repeatable; replaces the mcp "
                             "setting): context7=https://mcp.context7.com/mcp. A key in the driver's "
                             "NAME_API_KEY is sent as a bearer token, added by the gateway for a sandbox.")
    engine.add_argument("--claude-json", dest="claude_json",
                        help="JSON file merged into the target's ~/.claude.json (engine=claude-code).")
    engine.add_argument("--claude-settings", dest="claude_settings",
                        help="JSON file merged into the target's ~/.claude/settings.json (engine=claude-code).")
    engine.add_argument("--permission-mode", dest="permission_mode",
                        choices=["default", "acceptEdits", "plan"],
                        help="Claude Code permission mode (default: acceptEdits).")

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

    run = sub.add_parser("run", help="Start the task if its branch is new, else resume it. For Jobs and cron.")
    run.add_argument("slug")
    run.add_argument("--task", help="What to do. Needed only when the task has not started yet.")
    run.add_argument("--message", help="Instruction for this burst, when resuming.")
    run.set_defaults(func=cmd_run)

    chat = sub.add_parser("chat", help="Drive a task interactively, starting it if needed.")
    chat.add_argument("slug")
    chat.set_defaults(func=cmd_chat)

    ls = sub.add_parser("list", help="Show containers, sandbox claims, and harness Jobs in flight.")
    ls.set_defaults(func=cmd_list)

    sess = sub.add_parser("session", help="Long-lived interactive sandboxes with a web UI (podman, or "
                                          "agent-sandbox behind the cluster's gateway).")
    sess.add_argument("action", choices=["start", "stop", "rm", "list", "reap"],
                      help="start: start it, or show a running one. stop: remove the containers, keep the "
                           "workspace. rm: delete the workspace too. list: sessions and where they are. "
                           "reap (on a cluster): stop each session idle for longer than its --idle.")
    sess.add_argument("name", nargs="?", help="The session's name.")
    sess.add_argument("--port", type=int, help="Host port for the UI (default: a free one, then the same one).")
    sess.add_argument("--bind", help="Host address the UI listens on (default 127.0.0.1; a LAN address or "
                                     "0.0.0.0 to reach it from other machines).")
    sess.add_argument("--agent", choices=["opencode", "claude-code"],
                      help="What you talk to: opencode's web UI, or Claude Code in a terminal in the browser. "
                           "Default: as the session was last started; opencode for a new one.")
    sess.add_argument("--allow", help="The session's own allowlist entries, comma-separated (host or host:port), "
                                      "kept with it and added to the defaults each start. Replaces what it had.")
    sess.add_argument("--terminal", action=argparse.BooleanOptionalAction, default=None,
                      help="Also serve a terminal in the browser (zellij), behind its own login token. "
                           "Default: as the session was last started; off for a new one.")
    sess.add_argument("--terminal-port", type=int, dest="terminal_port", help="Host port for the terminal.")
    sess.add_argument("--idle", help="Stop the session after this long with nothing going in or out: 90m, 2h, "
                                     "1d, or seconds. 0 for never. Default: as last started; never for a new one.")
    sess.add_argument("--lifetime", help="Stop the session this long after it starts, whatever it is doing. "
                                         "Same forms as --idle.")
    sess.add_argument("--opencode-config", dest="opencode_config",
                      help="An opencode.json to use as is, instead of one written from the harness's model settings.")
    sess.set_defaults(func=cmd_session)

    eg = sub.add_parser("egress", help="See what a gateway refused, and approve it.")
    eg.add_argument("action", choices=["pending", "approve", "allow", "revoke", "list"],
                    help="pending: refusals on running gateways. approve ID: let one through. allow / revoke "
                         "HOST[:PORT]: for good. list: what is allowed for good.")
    eg.add_argument("what", nargs="?", help="A refusal's id (approve), or a host (allow, revoke).")
    eg.add_argument("--scope", default="session",
                    help="For approve: once, session (until that gateway stops; the default), a duration "
                         "(90m, 2h, 1d), or permanent.")
    eg.set_defaults(func=cmd_egress)

    jb = sub.add_parser("job", help="Run a job: a directory with job.toml and prompt.md, ending in a report.")
    jb.add_argument("action", choices=["run", "check", "runs"],
                    help="run: one run, in a fresh container, its report sent to the job's outputs. "
                         "check: show what a run would use, and what is missing. "
                         "runs: its recent runs.")
    jb.add_argument("dir", help="The job's directory.")
    jb.set_defaults(func=cmd_job)

    release = sub.add_parser("release",help="Remove a kept target (--claim, or --docker-container).")
    release.set_defaults(func=cmd_release)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
