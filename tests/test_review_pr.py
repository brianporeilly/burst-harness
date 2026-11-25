"""A review of an open PR, asked for from the orchestrator (`harness review`, review.of_pr): a reviewer with a fresh
context, read-only on the PR's branch. It ends with a verdict, posted as a review by the forge's reviewer (who can
approve or request changes), or as a comment when there is no reviewer token. Nothing is committed or pushed."""

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


@pytest.fixture(autouse=True)
def earlier(monkeypatch):
    """What was said on the PR before the review: nothing, unless a test says; never the real forge."""
    said = []
    monkeypatch.setattr(git, "feedback", lambda url, cfg=None, since="": list(said))
    return said


@pytest.fixture
def comments(monkeypatch):
    posted = []
    monkeypatch.setattr(git, "review_pr", lambda url, verdict, body, cfg=None: None)  # no reviewer token

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
    assert result["said"] == {"review": "1. f.py:2 returns 1; the task asks for 2.", "verdict": "comment",
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
    assert comments[0][1].endswith("No findings: nothing worth changing.\n\n**Verdict:** approve.")
    assert result["said"]["verdict"] == "approve"
    assert "VERDICT: REQUEST_CHANGES" in json.dumps(requests[0]["messages"])  # a profile is told the rules too


def test_a_comment_that_cant_be_posted_is_said_and_the_findings_kept(tmp_path, origin, pushed, fake_claude,  # noqa: F811
                                                                      monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "review")
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "A finding.")

    def refused(url, body, cfg=None):
        raise git.GitError("gh api failed: HTTP 403")

    monkeypatch.setattr(git, "comment_pr", refused)
    monkeypatch.setattr(git, "review_pr", lambda url, verdict, body, cfg=None: None)
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
    monkeypatch.setattr(git, "_gh", lambda args, what, as_token="": said.append(args) or "https://github.com/o/r/pull/7#c\n")
    assert git.comment_pr(PR, "**Review**") == "https://github.com/o/r/pull/7#c"
    assert said == [["api", "repos/o/r/issues/7/comments", "-f", "body=**Review**", "--jq", ".html_url"]]

    from harness.config import HarnessConfig

    cfg = HarnessConfig(forges=("forgejo=https://forgejo.example.com",))
    asked = []
    monkeypatch.setattr(forge, "_api", lambda host, method, path, body=None, token=None: asked.append((method, path, body))
                        or (201, {"html_url": "https://forgejo.example.com/o/r/pulls/3#issuecomment-9"}))
    url = git.comment_pr("https://forgejo.example.com/o/r/pulls/3", "**Review**", cfg)
    assert url.endswith("#issuecomment-9")
    assert asked == [("POST", "/repos/o/r/issues/3/comments", {"body": "**Review**"})]


def test_the_verdict_is_read_from_the_reviews_last_line():
    assert review.verdict("1. **blocking** f.py:2 returns 1.\n\nVERDICT: REQUEST_CHANGES") == (
        "request_changes", "1. **blocking** f.py:2 returns 1.")
    assert review.verdict("1. **nit** a name.\n**VERDICT: APPROVE**\n") == ("approve", "1. **nit** a name.")
    assert review.verdict("A finding.\n`Verdict: request changes.`")[0] == "request_changes"
    assert review.verdict("NO FINDINGS") == ("approve", "No findings: nothing worth changing.")
    assert review.verdict("VERDICT: APPROVE, but see above.\nMore text.") == (
        "comment", "VERDICT: APPROVE, but see above.\nMore text.")  # only a last line of its own counts
    assert review.verdict("A finding.") == ("comment", "A finding.")


def test_the_review_is_posted_as_the_reviewer_with_its_verdict(tmp_path, origin, pushed, fake_claude,  # noqa: F811
                                                               monkeypatch, capsys, earlier):
    earlier += [{"who": "review-bot", "when": "2026-11-25T10:00:00Z", "where": "review (request_changes)",
                 "text": "1. **blocking** f.py:2 returns 1."},
                {"who": "coding-bot", "when": "2026-11-25T11:00:00Z", "where": "", "text": "Declined: the plan says 1."}]
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "review")
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "The earlier point was answered.\n\nVERDICT: APPROVE")
    reviewed, commented = [], []
    monkeypatch.setattr(git, "review_pr", lambda url, verdict, body, cfg=None: reviewed.append((url, verdict, body))
                        or f"{url}#issuecomment-5")
    monkeypatch.setattr(git, "comment_pr", lambda url, body, cfg=None: commented.append(body) or url)
    cli.main(["--repo-url", str(origin), "--repo", str(tmp_path / "w"), "--engine", "claude-code",
              "review", "t1", "--pr", PR])
    result = _result(capsys.readouterr().out)
    assert {k: v for k, v in result["said"].items() if k != "by"} == {
        "review": "The earlier point was answered.", "verdict": "approve", "posted": f"{PR}#issuecomment-5"}
    [(url, verdict, body)] = reviewed
    assert url == PR and verdict == "approve" and body.endswith("answered.\n\n**Verdict:** approve.")
    assert commented == []
    [argv] = fake_claude()
    prompt = argv[argv.index("-p") + 1]
    assert "Earlier on this PR, oldest first" in prompt
    assert prompt.index("review-bot on review (request_changes)") < prompt.index("coding-bot (2026-11-25T11:00:00Z)")


def test_a_review_the_forge_refuses_becomes_a_comment(tmp_path, origin, pushed, fake_claude, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "review")
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "1. **blocking** a bug.\nVERDICT: REQUEST_CHANGES")

    def refused(url, verdict, body, cfg=None):
        raise git.GitError("forgejo refused the review (422): approve your own pull is not allowed")

    commented = []
    monkeypatch.setattr(git, "review_pr", refused)
    monkeypatch.setattr(git, "comment_pr", lambda url, body, cfg=None: commented.append(body) or url + "#c")
    cli.main(["--repo-url", str(origin), "--repo", str(tmp_path / "w"), "--engine", "claude-code",
              "review", "t1", "--pr", PR])
    captured = capsys.readouterr()
    result = _result(captured.out)
    assert result["said"]["verdict"] == "request_changes" and result["said"]["comment"] == PR + "#c"
    assert commented[0].endswith("**Verdict:** changes requested.")
    assert "Could not post the review as the reviewer" in captured.err


def test_reviews_go_to_the_prs_forge_as_its_reviewer(monkeypatch):
    from harness.config import HarnessConfig

    cfg = HarnessConfig(forges=("forgejo=https://forgejo.example.com",))
    fpr = "https://forgejo.example.com/o/r/pulls/3"
    asked = []
    monkeypatch.setattr(forge, "_api", lambda host, method, path, body=None, token=None:
                        asked.append((method, path, body, token)) or (200, {"html_url": fpr + "#issuecomment-4"}))
    monkeypatch.delenv("FORGEJO_REVIEWER_TOKEN", raising=False)
    monkeypatch.setenv("FORGEJO_TOKEN", "author-secret")
    assert git.review_pr(fpr, "approve", "Fine.", cfg) is None and asked == []  # no reviewer: none posted

    monkeypatch.setenv("FORGEJO_REVIEWER_TOKEN", "reviewer-secret")
    assert git.review_pr(fpr, "request_changes", "A bug.", cfg) == fpr + "#issuecomment-4"
    assert asked == [("POST", "/repos/o/r/pulls/3/reviews", {"event": "REQUEST_CHANGES", "body": "A bug."},
                      "reviewer-secret")]
    assert "reviewer-secret" in forge.all_tokens(cfg) and "author-secret" in forge.all_tokens(cfg)

    said = []
    monkeypatch.setattr(git, "_gh", lambda args, what, as_token="": said.append((args, as_token)) or PR + "#r\n")
    monkeypatch.setenv("GITHUB_REVIEWER_TOKEN", "gh-reviewer")
    assert git.review_pr(PR, "approve", "Fine.", cfg) == PR + "#r"
    assert said == [(["api", "repos/o/r/pulls/7/reviews", "-f", "event=APPROVE", "-f", "body=Fine.",
                      "--jq", ".html_url"], "gh-reviewer")]
