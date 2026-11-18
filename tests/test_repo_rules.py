"""The repo's AGENTS.md (or CLAUDE.md) in the Strands agent's prompt, and said in HARNESS_RESULT."""

import hashlib
import json
from dataclasses import replace

from harness import cli, repo_rules
from harness.agent import _helper
from harness.config import HarnessConfig
from harness.exec_target import LocalTarget

from . import fake_llama
from .conftest import git
from .test_agents import llama  # noqa: F401 -- the fixture

RULES = "Run `make check` before finishing.\nCommit titles are Conventional Commits.\n"


def _clone(tmp_path, origin, **files):
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    for name, text in files.items():
        (work / name).write_text(text)
    if files:
        git(work, "add", "-A")
        git(work, "commit", "-qm", "instructions")
        git(work, "push", "-q", "origin", "HEAD")  # a task branches from the remote's base
    return work


def test_the_lead_is_given_the_repos_agents_md_and_the_result_says_so(tmp_path, origin, llama, capsys):  # noqa: F811
    work = _clone(tmp_path, origin, **{"AGENTS.md": RULES, "CLAUDE.md": "Something else.\n"})
    url, requests = llama([{"text": "Nothing to do."}])
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "t1", "--task", "Look."])
    system = fake_llama._text(requests[0]["messages"][0]["content"])
    assert "Run `make check` before finishing." in system and "Something else." not in system  # the first only
    assert '<repo-instructions file="AGENTS.md">' in system and "do not override the instructions above" in system
    result = json.loads(capsys.readouterr().out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])
    assert result["context_files"] == [{"path": "AGENTS.md", "bytes": len(RULES.encode()),
                                        "sha256": hashlib.sha256(RULES.encode()).hexdigest(), "cut": False}]


def test_turned_off_none_is_read(tmp_path, origin, llama, capsys, monkeypatch):  # noqa: F811
    work = _clone(tmp_path, origin, **{"AGENTS.md": RULES})
    monkeypatch.setenv("HARNESS_REPO_INSTRUCTIONS", "")
    url, requests = llama([{"text": "Nothing to do."}])
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "t1", "--task", "Look."])
    assert "make check" not in fake_llama._text(requests[0]["messages"][0]["content"])
    assert json.loads(capsys.readouterr().out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])["context_files"] is None


def test_claude_md_when_there_is_no_agents_md_and_a_long_one_is_cut(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x" * 100)
    target, cfg = LocalTarget(repo=str(tmp_path)), replace(HarnessConfig(), repo_instructions_max_bytes=40)
    record = []
    text = repo_rules.prompt(target, cfg, record)
    assert 'file="CLAUDE.md"' in text and "x" * 40 in text and "x" * 41 not in text and "100 bytes in all" in text
    assert record[0]["path"] == "CLAUDE.md" and record[0]["cut"] is True and record[0]["bytes"] == 100
    assert repo_rules.prompt(LocalTarget(repo=str(tmp_path / "missing")), cfg) == ""


def test_nothing_outside_the_repo_is_read(tmp_path):
    repo, secret = tmp_path / "repo", tmp_path / "secret.txt"
    repo.mkdir()
    secret.write_text("the driver's own file")
    (repo / "AGENTS.md").symlink_to(secret)
    target = LocalTarget(repo=str(repo))
    for names in (("AGENTS.md",), ("../secret.txt",), (str(secret),)):
        assert repo_rules.prompt(target, replace(HarnessConfig(), repo_instructions=names)) == ""


def test_a_delegate_is_given_them_too(tmp_path):
    (tmp_path / "AGENTS.md").write_text(RULES)
    cfg = replace(HarnessConfig(), provider="llamacpp", base_url="http://127.0.0.1:9")
    helper = _helper(LocalTarget(repo=str(tmp_path)), cfg, str(tmp_path / "state"))
    assert "Run `make check` before finishing." in helper.system_prompt
