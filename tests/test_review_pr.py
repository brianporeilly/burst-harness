"""A review of an open PR, asked for from the orchestrator (`harness review`, review.of_pr): a reviewer with a fresh
context, read-only on the PR's branch, its findings a comment on the PR. Nothing is committed or pushed."""

import json

import pytest

from harness import cli, forge, git, review

from .conftest import git as sh_git
from .test_agents import llama  # noqa: F401 -- the fixture
from .test_claude_code import fake_claude  # noqa: F401 -- the fixture

PR = "https://github.com/o/r/pull/7"


def _result(out):
    return json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])


@pytest.fixture
def pushed(tmp_path, origin):
    """origin with the task's branch on it, a change of its own: the PR's."""
    work = tmp_path / "author"
    sh_git(tmp_path, "clone", "-q", str(origin), str(work))
    sh_git(work, "checkout", "-q", "-b", "harness/t1")
    (work / "f.py").write_text("def f():\n    return 1\n")
    sh_git(work, "add", "f.py")
    sh_git(work, "-c", "user.name=a", "-c", "user.email=a@example.com", "commit", "-q", "-m", "add f")
    sh_git(work, "push", "-q", "origin", "harness/t1")
    return sh_git(origin, "rev-parse", "harness/t1").strip()


@pytest.fixture
def comments(monkeypatch):
    posted = []

    def comment(url, body, cfg=None):
        posted.append((url, body))
        return f"{url}#issuecomment-1"

    monkeypatch.setattr(git, "comment_pr", comment)
    return posted


def test_claude_code_reviews_the_pr_and_its_findings_are_commented(tmp_path, origin, pushed, comments, fake_claude,  # noqa: F811
                                                                    monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "review")
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "1. f.py:2 returns 1; the task asks for 2.")
    code = cli.main(["--repo-url", str(origin), "--repo", str(tmp_path / "w"), "--engine", "claude-code",
                     "--model", "claude-sonnet-5-5", "review", "t1", "--pr", PR])
    result = _result(capsys.readouterr().out)
    assert code == cli.EXIT_DONE and result["state"] == "reviewed"
    assert result["said"] == {"review": "1. f.py:2 returns 1; the task asks for 2.",
                              "by": "Claude Code (claude-sonnet-5-5)", "comment": f"{PR}#issuecomment-1"}
    [(url, body)] = comments
    assert url == PR and body.startswith("**Review by Claude Code (claude-sonnet-5-5)**, asked for from the "
                                         "orchestrator") and body.endswith("the task asks for 2.")
    [argv] = fake_claude()
    prompt = argv[argv.index("-p") + 1]
    assert prompt.startswith(f"Review pull request {PR}: the change on this branch, against origin/main.")
    assert "return 1" in prompt  # the PR's diff, from its branch
    assert argv[argv.index("--append-system-prompt") + 1] == review.PR_SYSTEM  # not a task's rules
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5" and "--max-turns" in argv
    assert sh_git(origin, "rev-parse", "harness/t1").strip() == pushed  # nothing pushed, whatever it wrote


def test_a_profile_reviews_the_pr_read_only(tmp_path, origin, pushed, comments, llama, capsys):  # noqa: F811
    reviewer, requests = llama([{"text": "NO FINDINGS"}])
    scenario = tmp_path / "scenario.toml"
    scenario.write_text(f'[agents.reviewer]\nprovider = "llamacpp"\nbase_url = "{reviewer}"\nmodel_id = "r"\n'
                        'use_for = "reviewing"\n')
    cli.main(["--config", str(scenario), "--repo-url", str(origin), "--repo", str(tmp_path / "w"),
              "review", "t1", "--pr", PR, "--by", "reviewer"])
    result = _result(capsys.readouterr().out)
    assert result["state"] == "reviewed" and result["said"]["by"] == "reviewer"
    assert "Review pull request" in json.dumps(requests[0]["messages"])
    assert comments[0][1].endswith("No findings: nothing worth changing.")


def test_a_comment_that_cant_be_posted_is_said_and_the_findings_kept(tmp_path, origin, pushed, fake_claude,  # noqa: F811
                                                                      monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "review")
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "A finding.")

    def refused(url, body, cfg=None):
        raise git.GitError("gh api failed: HTTP 403")

    monkeypatch.setattr(git, "comment_pr", refused)
    cli.main(["--repo-url", str(origin), "--repo", str(tmp_path / "w"), "--engine", "claude-code",
              "review", "t1", "--pr", PR])
    result = _result(capsys.readouterr().out)
    assert result["state"] == "reviewed" and result["said"]["review"] == "A finding."
    assert result["said"]["reason"] == "the comment could not be posted: gh api failed: HTTP 403"


def test_no_branch_no_review_and_strands_needs_a_profile(tmp_path, origin, pushed, comments):
    with pytest.raises(SystemExit, match="harness/gone is not on origin"):
        cli.main(["--repo-url", str(origin), "--repo", str(tmp_path / "a"), "--engine", "claude-code",
                  "review", "t1", "--pr", PR, "--branch", "harness/gone"])
    with pytest.raises(SystemExit, match="A review needs a profile"):
        cli.main(["--repo-url", str(origin), "--repo", str(tmp_path / "b"), "review", "t1", "--pr", PR])
    assert comments == []


def test_comments_go_to_the_prs_forge(monkeypatch):
    said = []
    monkeypatch.setattr(git, "_gh", lambda args, what: said.append(args) or "https://github.com/o/r/pull/7#c\n")
    assert git.comment_pr(PR, "**Review**") == "https://github.com/o/r/pull/7#c"
    assert said == [["api", "repos/o/r/issues/7/comments", "-f", "body=**Review**", "--jq", ".html_url"]]

    from harness.config import HarnessConfig

    cfg = HarnessConfig(forges=("forgejo=https://forgejo.example.com",))
    asked = []
    monkeypatch.setattr(forge, "_api", lambda host, method, path, body=None: asked.append((method, path, body))
                        or (201, {"html_url": "https://forgejo.example.com/o/r/pulls/3#issuecomment-9"}))
    url = git.comment_pr("https://forgejo.example.com/o/r/pulls/3", "**Review**", cfg)
    assert url.endswith("#issuecomment-9")
    assert asked == [("POST", "/repos/o/r/issues/3/comments", {"body": "**Review**"})]
