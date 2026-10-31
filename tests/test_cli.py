import argparse

import pytest

from harness import cli
from harness.tools import HarnessContext

from .conftest import git as sh_git


def _args(**kw):
    base = dict(
        provider=None, model=None, effort=None, max_tokens=None, base_url=None, base_branch=None,
        draft_pr=None, push=None, offload_max_tokens=None, target=None, repo_url=None, pod=None,
        namespace=None, container=None, kube_context=None, docker_container=None, pool=None,
        claim=None, claim_ttl=None, keep_claim=None, repo=None, slug="t",
        image=None, engine=None, claude_json=None, claude_settings=None, permission_mode=None,
        egress=None, egress_allow=None, gateway_image=None, egress_inject=None, root=None, egress_webhook=None, mcp=None,
        agent=None, delegate_to=None, router=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_open_clones_and_keys_state(origin, tmp_path):
    ctx = cli._open(_args(repo=str(tmp_path / "w"), repo_url=str(origin)))
    assert ctx.branch == "harness/t"
    assert ctx.state_key == "origin"
    assert not cli._branch_exists(ctx)


def test_open_reads_repo_config_from_clone(origin, tmp_path):
    seed = tmp_path / "s"
    sh_git(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / ".harness.toml").write_text('branch_prefix = "bot/"\n')
    sh_git(seed, "add", "-A")
    sh_git(seed, "commit", "-qm", "cfg")
    sh_git(seed, "push", "-q", "origin", "main")
    ctx = cli._open(_args(repo=str(tmp_path / "w"), repo_url=str(origin)))
    assert ctx.branch == "bot/t"


def test_agent_sandbox_needs_repo_url():
    with pytest.raises(SystemExit, match="--repo-url"):
        cli._open(_args(target="agent-sandbox", pool="p"))


@pytest.mark.parametrize("terminal,waiting,code", [
    ("pr", False, cli.EXIT_DONE),
    ("paused", False, cli.EXIT_DONE),
    (None, True, cli.EXIT_NEEDS_INPUT),
    (None, False, cli.EXIT_NO_TERMINAL_STATE),
])
def test_exit_codes(origin, tmp_path, capsys, terminal, waiting, code):
    ctx = cli._open(_args(repo=str(tmp_path / "w"), repo_url=str(origin)))
    ctx.terminal, ctx.waiting = terminal, waiting
    assert cli._finish(None, ctx) == code
    assert '"state": "%s"' % (terminal or ("waiting" if waiting else "none")) in capsys.readouterr().out


def test_a_run_that_dies_still_says_whether_it_kept_its_sandbox(origin, tmp_path, capsys, monkeypatch):
    import json

    monkeypatch.setattr(cli, "_open", lambda args: cli.HarnessContext(
        target=cli.LocalTarget(repo=str(tmp_path)), cfg=cli.HarnessConfig(), slug="t", branch="harness/t", state_key="k"))

    def close(ctx):
        ctx.kept = True

    def dies(ctx):
        raise RuntimeError("no key")

    monkeypatch.setattr(cli, "_close", close)
    with pytest.raises(RuntimeError):
        cli._session(_args(), dies)
    line = [x for x in capsys.readouterr().out.splitlines() if x.startswith("HARNESS_RESULT ")][-1]
    result = json.loads(line.split(" ", 1)[1])
    assert (result["state"], result["kept"], result["slug"]) == ("error", True, "t")


def test_git_auth_is_left_to_the_gateway_when_it_injects(monkeypatch):
    monkeypatch.setenv("HARNESS_GIT_TOKEN", "sekret")
    from harness.config import HarnessConfig
    assert cli._git_auth(HarnessConfig(), "https://github.com/o/r") != ()
    assert cli._git_auth(HarnessConfig(egress_inject=("git",)), "https://github.com/o/r") == ()


def test_session_needs_a_name_and_a_container_runtime_or_agent_sandbox(monkeypatch):
    with pytest.raises(SystemExit, match="needs a session name"):
        cli.main(["session", "stop"])
    with pytest.raises(SystemExit, match="podman, docker or agent-sandbox"):
        cli.main(["--target", "kubectl", "session", "start", "x"])
    with pytest.raises(SystemExit, match="needs pool, gateway_control, session_domain"):
        cli.main(["--target", "agent-sandbox", "--namespace", "s", "session", "start", "x"])
    with pytest.raises(SystemExit, match="reap is for sessions on a cluster"):
        cli.main(["--target", "podman", "session", "reap"])
    with pytest.raises(SystemExit, match="needs gateway_control"):
        cli.main(["--target", "agent-sandbox", "--namespace", "s", "session", "reap"])


def _usage(**usage):
    from types import SimpleNamespace

    return SimpleNamespace(event_loop_metrics=SimpleNamespace(accumulated_usage=usage))


def test_cache_share_counts_cached_tokens_once_whichever_provider():
    # Anthropic leaves cache reads out of input; llama.cpp counts them in it.
    anthropic = _usage(inputTokens=100, outputTokens=1, cacheReadInputTokens=900)
    assert "90% of input served from cache" in cli._report(anthropic, "m", "anthropic")
    llama = _usage(inputTokens=1000, outputTokens=1, cacheReadInputTokens=900)
    assert "90% of input served from cache" in cli._report(llama, "m", "llamacpp")
    assert "input 1,000 " in cli._report(llama, "m", "llamacpp")  # as the server counts it


def test_a_burst_whose_replies_keep_hitting_the_output_limit_ends_as_max_tokens(tmp_path, capsys, monkeypatch):
    import json

    from strands.types.exceptions import MaxTokensReachedException

    class CutOff:
        def __init__(self):
            self.messages, self.calls = [{"role": "user", "content": [{"text": "go"}]}], 0

        def __call__(self, prompt=None):
            self.calls += 1
            self.messages.append({"role": "assistant", "content": [{"text": "thinking..."}]})
            raise MaxTokensReachedException("cut")

    agent = CutOff()
    monkeypatch.setattr(cli, "_open", lambda args: cli.HarnessContext(
        target=cli.LocalTarget(repo=str(tmp_path)), cfg=cli.HarnessConfig(), slug="t", branch="harness/t", state_key="k"))
    monkeypatch.setattr(cli, "_close", lambda ctx: None)
    code = cli._session(_args(), lambda ctx: cli._run(agent, ctx, "go", interactive=False))
    out = capsys.readouterr()
    result = json.loads([x for x in out.out.splitlines() if x.startswith("HARNESS_RESULT ")][-1].split(" ", 1)[1])
    assert code == cli.EXIT_NO_TERMINAL_STATE and result["state"] == "max_tokens"
    assert agent.calls == 3 and [m["role"] for m in agent.messages] == ["user"]  # each cut-off reply dropped
    assert "hit the output limit 3 times in a row" in out.err


def test_a_sandbox_with_commits_not_pushed_is_kept(origin, tmp_path, capsys):
    work = tmp_path / "work"
    sh_git(tmp_path, "clone", "-q", str(origin), str(work))

    class Owned(cli.LocalTarget):
        owned = True
        released = False

        def release(self):
            self.released = True
            return "released"

    def close(target, terminal=None):
        ctx = cli.HarnessContext(target=target, cfg=cli.HarnessConfig(), slug="t", branch="harness/t", state_key="k")
        ctx.terminal = terminal
        cli._close(ctx)
        return ctx

    pushed = Owned(repo=str(work))
    assert not close(pushed).kept and pushed.released  # nothing that is not on the remote
    (work / "new.txt").write_text("work\n")
    sh_git(work, "add", "-A")
    sh_git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "work")
    ahead = Owned(repo=str(work))
    assert close(ahead).kept and not ahead.released
    assert "1 commit(s) not pushed" in capsys.readouterr().out
    abandoned = Owned(repo=str(work))
    assert not close(abandoned, "abandoned").kept and abandoned.released  # given up: let it go
