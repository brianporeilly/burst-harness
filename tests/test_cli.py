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
        egress=None, egress_allow=None, gateway_image=None, egress_inject=None, root=None,
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


def test_git_auth_is_left_to_the_gateway_when_it_injects(monkeypatch):
    monkeypatch.setenv("HARNESS_GIT_TOKEN", "sekret")
    from harness.config import HarnessConfig
    assert cli._git_auth(HarnessConfig(), "https://github.com/o/r") != ()
    assert cli._git_auth(HarnessConfig(egress_inject=("git",)), "https://github.com/o/r") == ()


def test_session_needs_a_name_and_podman(monkeypatch):
    with pytest.raises(SystemExit, match="needs a session name"):
        cli.main(["session", "stop"])
    with pytest.raises(SystemExit, match="podman for now"):
        cli.main(["--target", "agent-sandbox", "session", "start", "x"])
