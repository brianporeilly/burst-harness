"""engine=claude-code: Claude Code does the work inside the target; the harness
does everything around it.

The harness still claims or starts the target, clones, creates the branch,
commits, pushes, opens the PR from the driver, and releases the target. What
changes is the agent loop: instead of a Strands agent driven from outside,
`claude` runs in the target --

* headless (`start`, `resume`, `run`): ``claude -p`` with ``--output-format
  stream-json``, streamed back and summarised. Claude cannot call the harness's
  terminal-state tools, so it ends by writing an outcome file
  (``.harness/outcome.json``: finish, pause, or abandon) that the harness then
  applies exactly as the Strands tools would.
* interactive (`chat`): ``claude`` in a tmux session, attached to this terminal.
  Detach with the usual tmux keys and the session keeps running; `chat` again
  reattaches. When the session ends, its outcome (or, failing that, a pause if
  work is uncommitted) is applied.

Permissions are Claude Code's own: headless runs use ``claude_permission_mode``
(default ``acceptEdits``), and what else Claude may run comes from the settings
you supply (``claude_settings``, merged into the target's
``~/.claude/settings.json``). ``bypassPermissions`` lets Claude run anything
without asking, which is what a sandbox is for: it is refused on the local
target, where Claude would run as you on your machine, and orchestrator tasks
(always in a sandbox) use it. The harness answers no prompts on your behalf.
The git rules (git_policy.py) are added to every invocation as a PreToolUse
hook via ``--settings``; a hook that refuses a command blocks it in every
permission mode, bypassPermissions included.

With ``delegate_to``, Claude Code leads and hands pieces of the work to Strands
profiles through a ``delegate`` tool: headless runs then go through the Claude
Agent SDK, its tool answered by the driver (see claude_lead.py).

Credentials: unlike the Strands engine, the agent loop runs *in* the target, so
model credentials must be there while it runs. They are written to a 0600 file
in the target from the driver's environment (never argv) and removed afterwards.
The git token still never enters the target.

With ``egress_inject = model`` they are not there either: the file holds
placeholders, and the target's gateway puts the real credential in their place
on requests to the API (see containers.py).

A provider plugin with a ``target_endpoint`` (models.py) replaces all of that:
Claude Code talks to the plugin's endpoint (ANTHROPIC_BASE_URL), and none of
the driver's Anthropic settings are passed. Its token, which may last only an
hour, is in a 0600 file that Claude Code reads through ``apiKeyHelper``, asking
again every few minutes and after a 401; a thread on the driver asks the
plugin for the token while Claude Code runs (at half its remaining life, and
at least once a minute) and rewrites the file when it changes. Not yet with
``egress_inject = model``, which is refused.
"""

from __future__ import annotations

import json
import shlex
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import agents, credentials, git, git_policy, mcp, models, state, terminal
from .agent_sandbox import label_value
from .containers import MODEL_CREDENTIALS
from .context import HarnessContext
from .exec_target import LocalTarget, SubprocessTarget

OUTCOME_PATH = ".harness/outcome.json"
# Harness-owned scratch space in a remote target. Outside the repo, so nothing
# here can be committed.
REMOTE_DIR = "/tmp/burst-harness"
ENV_FILE = f"{REMOTE_DIR}/claude.env"
# Forwarded from the driver's environment into a remote target, when set.
CREDENTIAL_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
# A provider plugin's target endpoint: its token, which Claude Code reads with apiKeyHelper this often (and
# after a 401), and the longest the driver goes without asking the plugin for it.
TOKEN_FILE = f"{REMOTE_DIR}/model-token"
HELPER_TTL_MS = 5 * 60 * 1000
REFRESH_MAX_SECONDS = 60

RULES = """You are working on one task in a git repository, driven by burst-harness.

Branch: {branch} -- already created from {remote}/{base}. Do not create or switch branches, and do not push: the harness commits, pushes, and opens pull requests.

Task document: {doc} -- committed, and the authoritative record of this task. Read it first and trust it over re-deriving context from the codebase. Keep it current as you work, in its existing sections: Goal, Constraints, Done (outcomes, terse), Decisions (why), Open questions, Next step (one item). Keep it under {cap} bytes: summarise rather than append.

When you stop, write {outcome} -- a JSON object in exactly one of these shapes -- and then end your turn:
  {{"state": "finish", "title": "PR title", "body": "what changed and why", "next_step": "optional follow-up"}}  -- the work is reviewable; the harness opens a PR.
  {{"state": "pause", "summary": "what was accomplished", "next_step": "the single next step", "questions": "optional: what a human must decide"}}  -- mid-flight, or blocked on a decision.
  {{"state": "abandon", "reason": "why"}}  -- the task should not continue.
If unsure which applies, pause. You may commit as you go; anything left uncommitted is committed for you."""

NO_OUTCOME = """You stopped without writing {outcome}. Write it now (finish, pause, or abandon, in the shapes given in your instructions), then end your turn."""

BAD_OUTCOME = """{outcome} could not be applied: {error}
Fix the problem and write {outcome} again, then end your turn."""


# Claude Code's headless permission modes; BYPASS runs anything without asking, only in a sandbox.
BYPASS = "bypassPermissions"
PERMISSION_MODES = ("default", "acceptEdits", "plan", BYPASS)


class ClaudeCodeError(RuntimeError):
    """The target cannot run Claude Code, or its setup failed."""


class TokenKeeper:
    """Keeps a target endpoint's token current in a file in the target, while Claude Code runs."""

    def __init__(self, endpoint: models.TargetEndpoint, write: Callable[[str], None]) -> None:
        self.endpoint, self.write = endpoint, write
        self.current = ""
        self.expires: float | None = None
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None

    def refresh(self) -> None:
        """Ask the plugin for the token, and write it if it changed."""
        got = self.endpoint.token()
        token, expires = (got, None) if isinstance(got, str) else (getattr(got, "token", ""), getattr(got, "expires_at", None))
        if not isinstance(token, str) or not token.strip():
            raise ClaudeCodeError("the provider's target endpoint gave no token")
        if token != self.current:
            self.write(token.strip())
            self.current = token
        self.expires = float(expires) if expires is not None else None

    def delay(self) -> float:
        """Until the next ask: half the token's remaining life, at least a second, at most REFRESH_MAX_SECONDS."""
        if self.expires is None:
            return REFRESH_MAX_SECONDS
        return max(1.0, min(REFRESH_MAX_SECONDS, (self.expires - time.time()) / 2))

    def start(self) -> None:
        self.refresh()  # the first token, before Claude Code starts: a failure here stops the burst
        self.thread = threading.Thread(target=self._keep, name="token-keeper", daemon=True)
        self.thread.start()

    def _keep(self) -> None:
        while not self.stopping.wait(self.delay()):
            try:
                self.refresh()
            except Exception as err:  # noqa: BLE001 -- said, and tried again; Claude Code has the last token
                self.expires = None
                print(f"! Could not refresh the model endpoint's token: {type(err).__name__}: {err}", file=sys.stderr)

    def stop(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=10)


class ClaudeCodeEngine:
    """Runs one task's Claude Code sessions in `ctx.target`."""

    # Keeping a provider plugin's endpoint token current, from prepare() to cleanup(); None without one.
    keeper: TokenKeeper | None = None
    # delegate(to, task) for cfg.delegate_to's profiles, from prepare(); None without them.
    delegate: Callable[[str, str], str] | None = None

    def __init__(self, ctx: HarnessContext) -> None:
        if not isinstance(ctx.target, SubprocessTarget):
            raise ClaudeCodeError(f"engine=claude-code cannot drive {ctx.target.describe()}.")
        self.ctx = ctx
        self.target: SubprocessTarget = ctx.target
        self.remote = not isinstance(ctx.target, LocalTarget)
        self.has_env_file = False
        self.hook_path = ""
        self.result: dict[str, Any] | None = None
        self.cost = 0.0
        self.turns = 0

    # --- setup -----------------------------------------------------------

    def prepare(self) -> None:
        """Check the target, install the git hook and credentials, apply your config."""
        cfg = self.ctx.cfg
        if cfg.claude_permission_mode not in PERMISSION_MODES:
            raise ClaudeCodeError(f"claude_permission_mode is one of {', '.join(PERMISSION_MODES)}, "
                                  f"not {cfg.claude_permission_mode!r}.")
        if cfg.claude_permission_mode == BYPASS and not self.remote:
            raise ClaudeCodeError(f"claude_permission_mode {BYPASS} runs anything without asking: only in a sandbox "
                                  "(a container or a cluster target), not on the local target.")
        if missing := self.target.missing_commands(("claude", "python3")):
            raise ClaudeCodeError(
                f"engine=claude-code needs {', '.join(missing)} in {self.target.describe()} "
                "(the burst-harness-sandbox image has both)."
            )
        self._exclude_outcome()
        if cfg.delegate_to:
            from .agent import delegate_function
            from .claude_lead import SERVER

            if SERVER in mcp.servers(cfg):
                raise ClaudeCodeError(f"mcp {SERVER} is the name of the harness's own server, for delegate_to: "
                                      "rename yours.")
            state_dir = self.state_dir()
            state_dir.mkdir(parents=True, exist_ok=True)
            self.delegate = delegate_function(self.target, cfg, self.ctx.delegated, str(state_dir))
            print(f"Claude Code may delegate to: {', '.join(agents.describe(cfg, n) for n in cfg.delegate_to)}.")
        if not self.remote:
            # Local: Claude runs as you, with your own ~/.claude config and
            # environment. The harness does not touch either.
            self.hook_path = git_policy.__file__
            if cfg.claude_json or cfg.claude_settings:
                print("Local target: ignoring claude_json/claude_settings; your own ~/.claude config applies.")
            return

        self._upload(f"{REMOTE_DIR}/git_policy.py", Path(git_policy.__file__).read_text())
        self.hook_path = f"{REMOTE_DIR}/git_policy.py"

        endpoint = models.target_endpoint(cfg)
        if endpoint is not None:
            # The plugin's endpoint and token, and none of the driver's Anthropic settings: a developer's
            # shell often has its own ANTHROPIC_BASE_URL, and mixing the two sends credentials astray.
            if "model" in cfg.egress_inject:
                raise ClaudeCodeError(f"provider {cfg.provider} gives Claude Code its own endpoint and token, "
                                      "which egress_inject = model cannot hold yet: drop model from egress_inject.")
            self.keeper = TokenKeeper(endpoint, lambda token: self._upload(TOKEN_FILE, token))
            self.keeper.start()
            creds = {"ANTHROPIC_BASE_URL": endpoint.base_url, "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": str(HELPER_TTL_MS),
                     **endpoint.env}
            print(f"Claude Code talks to {endpoint.base_url} (provider {cfg.provider}), its token kept current.")
        else:
            creds = {name: value for name in CREDENTIAL_VARS if (value := credentials.get(name))}
        if "model" in cfg.egress_inject:
            # The gateway holds the real ones and swaps them in for these.
            creds = {name: MODEL_CREDENTIALS[name][1] if name in MODEL_CREDENTIALS else value
                     for name, value in creds.items()}
            print("Model credentials stay at the gateway; the target has placeholders.")
        if creds:
            self._upload(ENV_FILE, "".join(f"export {k}={shlex.quote(v)}\n" for k, v in creds.items()))
            self.has_env_file = True
        else:
            print(
                f"! No {' / '.join(CREDENTIAL_VARS[:2])} in the driver's environment; "
                "Claude Code will use whatever login the target already has.",
                file=sys.stderr,
            )

        seeds = []
        for flag, source in (("--claude-json", cfg.claude_json), ("--settings", cfg.claude_settings)):
            if not source:
                continue
            path = Path(source).expanduser()
            try:
                content = path.read_text()
                if not isinstance(json.loads(content), dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as err:
                raise ClaudeCodeError(f"{path}: {err}") from err
            remote = f"{REMOTE_DIR}/seed-{flag.strip('-')}.json"
            self._upload(remote, content)
            seeds += [flag, remote]
        if seeds:
            if self.target.missing_commands(("burst-sandbox-init",)):
                raise ClaudeCodeError(
                    "claude_json/claude_settings need burst-sandbox-init in the target "
                    "(the burst-harness-sandbox image has it)."
                )
            self.target.run_shell(shlex.join(["burst-sandbox-init", *seeds]), cwd="/")
            print("Applied your Claude Code config to the target.")

    def _exclude_outcome(self) -> None:
        # The outcome file is Claude's message to the harness, never a commit.
        self.target.run_shell(
            f'exclude="$(git rev-parse --git-path info/exclude)" && mkdir -p "$(dirname "$exclude")" '
            f'&& (grep -qxF {shlex.quote("/" + OUTCOME_PATH)} "$exclude" 2>/dev/null '
            f'|| echo {shlex.quote("/" + OUTCOME_PATH)} >> "$exclude") && rm -f {shlex.quote(OUTCOME_PATH)}'
        )

    def cleanup(self) -> None:
        """Remove the credentials file from a remote target, and the endpoint's token with its keeper."""
        if self.keeper is not None:
            self.keeper.stop()
            self.keeper = None
            self.target.run_shell(f"rm -f {shlex.quote(TOKEN_FILE)}", check=False, cwd="/")
        if self.has_env_file:
            self.target.run_shell(f"rm -f {shlex.quote(ENV_FILE)}", check=False, cwd="/")
            self.has_env_file = False

    def state_dir(self) -> Path:
        """The driver's state for this run: where a delegate keeps what it offloads, for a local target."""
        return self.ctx.cfg.resolve_state_dir(self.ctx.state_key)

    def _upload(self, path: str, content: str) -> None:
        """Write a file in the target through stdin, so contents never touch argv."""
        parent = shlex.quote(str(Path(path).parent))
        self.target.run_shell(f"umask 077 && mkdir -p {parent} && cat > {shlex.quote(path)}", stdin=content, cwd="/")

    # --- invocation ------------------------------------------------------

    def rules(self) -> str:
        cfg = self.ctx.cfg
        return RULES.format(
            branch=self.ctx.branch, remote=cfg.remote, base=cfg.base_branch,
            doc=state.doc_relpath(cfg, self.ctx.slug), cap=cfg.doc_max_bytes, outcome=OUTCOME_PATH,
        )

    def hook_settings(self) -> str:
        cfg = self.ctx.cfg
        command = shlex.join([
            "python3", self.hook_path,
            "--protected", ",".join(cfg.protected_branches),
            "--remote", cfg.remote, "--base", cfg.base_branch,
        ])
        settings: dict[str, Any] = {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}
        ]}}
        if self.keeper is not None:
            settings["apiKeyHelper"] = f"cat {shlex.quote(TOKEN_FILE)}"
        return json.dumps(settings)

    def command(self, *args: str) -> str:
        """A shell string running `claude` with the harness's settings and yours."""
        cfg = self.ctx.cfg
        yours = shlex.split(cfg.claude_args)
        # model_id, unless claude_args picks one: without it Claude Code uses
        # its own default, whatever the harness's settings say.
        model = [] if any(arg == "--model" or arg.startswith("--model=") for arg in yours) else ["--model", cfg.model_id]
        # The MCP servers you configured, with their tools allowed: you chose them, and
        # nobody is there to approve each call.
        configured, allowed = (mcp.claude(cfg)["mcpServers"], mcp.claude_tools(cfg)) if mcp.servers(cfg) else ({}, [])
        lead = ""
        if self.delegate is not None:
            from .agent import LEAD_PROMPT
            from .claude_lead import SERVER, TOOL

            # Answered in the driver, over this claude's stdin and stdout (claude_lead.py).
            configured, allowed = {**configured, SERVER: {"type": "sdk", "name": SERVER}}, [*allowed, TOOL]
            lead = LEAD_PROMPT.format(roster=agents.roster(cfg, cfg.delegate_to))
        servers = ["--mcp-config", json.dumps({"mcpServers": configured}), "--allowedTools", ",".join(allowed)] \
            if configured else []
        argv = [
            "claude", *args, *model,
            "--permission-mode", cfg.claude_permission_mode,
            "--settings", self.hook_settings(),
            "--append-system-prompt", self.rules() + lead,
            *servers,
            *yours,
        ]
        env = f"set -a && . {shlex.quote(ENV_FILE)} && set +a && " if self.has_env_file else ""
        return env + shlex.join(argv)

    # --- headless --------------------------------------------------------

    def headless(self, prompt: str) -> None:
        """Run until Claude records an outcome the harness can apply.

        A failed `claude` invocation (not logged in, bad flag, API down) ends the
        run: asking again would fail the same way.
        """
        if not self._run(prompt, resume=False):
            return
        for attempt in range(self.ctx.cfg.max_fixup_attempts + 1):
            outcome, error = self._read_outcome()
            if outcome is not None:
                error = self.apply(outcome)
                if error is None:
                    return
            if attempt == self.ctx.cfg.max_fixup_attempts:
                break
            follow_up = BAD_OUTCOME.format(outcome=OUTCOME_PATH, error=error) if error else NO_OUTCOME.format(outcome=OUTCOME_PATH)
            if not self._run(follow_up, resume=True):
                return
        if git.is_dirty(self.target):
            print(f"\n! No usable outcome. Uncommitted work left as-is:\n{git.dirty_summary(self.target)}", file=sys.stderr)

    def _run(self, prompt: str, *, resume: bool) -> bool:
        """One `claude -p` invocation (a session through the SDK, when delegating). False if claude itself failed."""
        args = ["--output-format", "stream-json", "--verbose", *(["--continue"] if resume else [])]
        if self.delegate is not None:
            from . import claude_lead
            from .agent import delegate_description

            # The prompt goes on stdin, as the SDK's first message.
            argv = self.target._wrap(self.command(*args, *claude_lead.STREAM_INPUT), stdin=True)
            code = claude_lead.run(argv, prompt, self._on_line, self.delegate, delegate_description(self.ctx.cfg))
        else:
            code = self.target.stream_shell(self.command("-p", prompt, *args), self._on_line)
        if code != 0:
            print(f"! claude exited with status {code}; not retrying.", file=sys.stderr)
            return False
        return True

    def _on_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            print(line)
            return
        kind = event.get("type")
        if kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    print(block["text"].strip())
                elif block.get("type") == "tool_use":
                    print(f"  -> {block.get('name')}: {_brief(block.get('input'))}")
        elif kind == "result":
            self.result = event
            self.cost += event.get("total_cost_usd") or 0.0
            self.turns += event.get("num_turns") or 0
            if event.get("is_error"):
                print(f"! Claude Code: {event.get('subtype')}: {event.get('result') or ''}".rstrip(), file=sys.stderr)

    # --- outcomes --------------------------------------------------------

    def _read_outcome(self) -> tuple[dict[str, Any] | None, str | None]:
        text = self.target.read_file(OUTCOME_PATH)
        if text is None:
            return None, None
        self.target.run_shell(f"rm -f {shlex.quote(OUTCOME_PATH)}")
        try:
            outcome = json.loads(text)
        except ValueError as err:
            return None, f"not valid JSON ({err})"
        if not isinstance(outcome, dict):
            return None, "not a JSON object"
        return outcome, None

    def apply(self, outcome: dict[str, Any]) -> str | None:
        """Apply an outcome through the terminal-state functions. Returns an error to fix, or None."""
        ctx = self.ctx
        current = git.current_branch(ctx.target)
        if current != ctx.branch:
            return f"you are on '{current}', not the task branch '{ctx.branch}'. Switch back to it first."

        def field(name: str, required: bool = True) -> str | None:
            value = outcome.get(name)
            if value is None and not required:
                return None
            if not isinstance(value, str) or not value.strip():
                raise KeyError(name)
            return value

        try:
            match outcome.get("state"):
                case "finish":
                    print(terminal.finish(ctx, title=field("title"), body=field("body"), next_step=field("next_step", False)))
                case "pause":
                    print(terminal.pause(ctx, summary=field("summary"), next_step=field("next_step"),
                                         questions=field("questions", False)))
                case "abandon":
                    print(terminal.abandon(ctx, reason=field("reason")))
                case other:
                    return f'"state" must be "finish", "pause", or "abandon" (got {other!r}).'
        except KeyError as missing:
            return f"{missing.args[0]!r} is required for state {outcome.get('state')!r} and must be a non-empty string."
        except ValueError as err:  # the task document is over its cap
            return str(err)
        return None

    # --- interactive -----------------------------------------------------

    def session_name(self) -> str:
        return f"burst-{label_value(self.ctx.slug)}"

    def interactive(self) -> None:
        """Attach this terminal to Claude Code in the target, in tmux when available."""
        ctx = self.ctx
        if self.delegate is not None:
            print("! A chat has no delegate tool: delegate_to is for headless runs (run, start, resume, jobs).")
            self.delegate = None
        claude = self.command()
        use_tmux = not self.target.missing_commands(("tmux",))
        if use_tmux:
            name = self.session_name()
            print(f"Attaching to tmux session {name}. Detach with Ctrl-b d; it keeps running.")
            self.target.interactive_shell(f"tmux new-session -A -s {shlex.quote(name)} {shlex.quote(claude)}")
            if self.target.run_shell(f"tmux has-session -t {shlex.quote(name)}", check=False).returncode == 0:
                ctx.detached = True
                print(f"Detached; {name} is still running. Reattach: harness {ctx.target.attach_hint()} chat {ctx.slug}")
                return
        else:
            self.target.interactive_shell(claude)

        outcome, error = self._read_outcome()
        if outcome is not None:
            error = self.apply(outcome)
        if error:
            print(f"! Outcome not applied: {error}", file=sys.stderr)
        if not ctx.terminal and git.is_dirty(ctx.target):
            print(terminal.pause(
                ctx,
                summary="Interactive Claude Code session ended with uncommitted work.",
                next_step=f"Review the work and continue with `harness chat {ctx.slug}`.",
            ))

    # --- reporting -------------------------------------------------------

    def report(self) -> str:
        from .cli import _delegated

        if self.result is None:
            return "  no Claude Code result recorded" + _delegated(self.ctx.delegated)
        usage = self.result.get("usage") or {}
        return (
            f"  {self.turns} turn(s)  input {usage.get('input_tokens', 0):,}  "
            f"output {usage.get('output_tokens', 0):,}  "
            f"cache read {usage.get('cache_read_input_tokens', 0):,}  "
            f"cost ${self.cost:.3f} (as reported by Claude Code)"
        ) + _delegated(self.ctx.delegated)


def _brief(value: Any, limit: int = 120) -> str:
    """One line describing a tool call's input."""
    if isinstance(value, dict):
        for key in ("command", "file_path", "path", "pattern", "url", "description"):
            if isinstance(value.get(key), str):
                value = value[key]
                break
    text = value if isinstance(value, str) else json.dumps(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
