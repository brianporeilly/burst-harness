"""You as the reviewer (docs/project-layout.md, step 5): what you say on a task's open PR reaches its next burst,
a round that changed a project's roadmap waits for you after its merge, and a project's milestone shows on the
task's card and in Slack."""

import json

from harness import cli, forge, git
from harness.config import HarnessConfig
from orchestrator.slack import _project_lines

from .conftest import git as sh
from .test_agents import llama  # noqa: F401 -- the fixture
from .test_tasks import PR1, _runner, _settled, harness  # noqa: F401 -- the fixture

PR = "https://github.com/me/proj/pull/7"


def test_a_prs_comments_reviews_and_line_comments_are_read_after_the_last_burst(monkeypatch):
    pages = {
        "repos/me/proj/issues/7/comments": [
            [{"who": "brian", "bot": "User", "when": "2026-11-21T09:00:00Z", "where": "", "text": "Old, before the burst."}],
            [{"who": "brian", "bot": "User", "when": "2026-11-21T12:00:00Z", "where": "", "text": "Rename the CLI flag."},
             {"who": "ci", "bot": "Bot", "when": "2026-11-21T12:01:00Z", "where": "", "text": "Coverage 80%."}],
        ],
        "repos/me/proj/pulls/7/reviews": [[
            {"who": "brian", "bot": "User", "when": "2026-11-21T11:00:00Z", "where": "review (changes_requested)",
             "text": "Close, but see the comments."},
            {"who": "brian", "bot": "User", "when": "2026-11-21T11:00:00Z", "where": "review (commented)", "text": ""},
        ]],
        "repos/me/proj/pulls/7/comments": [[
            {"who": "dependabot[bot]", "bot": "User", "when": "2026-11-21T11:30:00Z", "where": "a.py:1", "text": "Bump."},
            {"who": "brian", "bot": "User", "when": "2026-11-21T11:05:00Z", "where": "src/f.py:12", "text": "Off by one."},
        ]],
    }
    asked = []

    def gh(args, what):
        asked.append(args)
        return "\n".join(json.dumps(page) for page in pages[args[2]])

    monkeypatch.setattr(git, "_gh", gh)
    said = git.feedback(PR, HarnessConfig(), since="2026-11-21T10:00:00+00:00")
    assert [(s["who"], s["where"], s["text"]) for s in said] == [
        ("brian", "review (changes_requested)", "Close, but see the comments."),
        ("brian", "src/f.py:12", "Off by one."),
        ("brian", "", "Rename the CLI flag."),
    ]
    assert all(args[:2] == ["api", "--paginate"] for args in asked)


def test_a_forgejo_prs_feedback(monkeypatch):
    host = forge.Forge(name="forgejo", kind="forgejo", host="forgejo.example.com", url="https://forgejo.example.com")
    answers = {
        "/repos/me/proj/issues/3/comments": [{"user": {"login": "brian"}, "created_at": "2026-11-21T12:00:00Z",
                                              "body": "Looks good otherwise."}],
        "/repos/me/proj/pulls/3/reviews": [{"id": 9, "user": {"login": "brian"}, "submitted_at": "2026-11-21T11:00:00Z",
                                            "state": "REQUEST_CHANGES", "body": "One thing."}],
        "/repos/me/proj/pulls/3/reviews/9/comments": [{"path": "src/f.py", "position": 4, "body": "Here.",
                                                       "created_at": "2026-11-21T11:00:01Z"}],
        "/repos/me/proj/pulls?state=open&limit=50": [{"head": {"ref": "harness/t"},
                                                       "html_url": "https://forgejo.example.com/me/proj/pulls/3"}],
    }
    monkeypatch.setattr(forge, "_api", lambda f, method, path, body=None: (200, answers[path]))
    said = forge.forgejo_feedback(host, "https://forgejo.example.com/me/proj/pulls/3")
    assert [(s["where"], s["text"]) for s in said] == [("", "Looks good otherwise."),
                                                       ("review (request_changes)", "One thing."), ("src/f.py:4", "Here.")]
    assert forge.forgejo_find_pr(host, "https://forgejo.example.com/me/proj", "harness/t").endswith("/pulls/3")
    assert forge.forgejo_find_pr(host, "https://forgejo.example.com/me/proj", "harness/other") is None


def test_the_next_burst_is_given_what_the_review_said(tmp_path, origin, llama, capsys, monkeypatch):  # noqa: F811
    pause = {"tool": "pause_for_review", "args": {"summary": "s", "next_step": "n"}}
    url, _ = llama([pause, {"text": "."}])
    work = tmp_path / "work"
    sh(tmp_path, "clone", "-q", str(origin), str(work))
    args = ["--provider", "llamacpp", "--base-url", url, "--repo", str(work)]
    cli.main([*args, "start", "t1", "--task", "Add f."])
    capsys.readouterr()

    monkeypatch.setattr(git, "repo_slug", lambda remote: "github.com/me/proj")
    monkeypatch.setattr(git, "find_pr", lambda repo, head, cfg=None: PR if head == "harness/t1" else None)
    asked_since = []
    monkeypatch.setattr(git, "feedback", lambda pr, cfg=None, since="": asked_since.append(since) or [
        {"who": "brian", "when": "2026-11-21T12:00:00Z", "where": "src/f.py:12", "text": "Off by one."}])
    url, requests = llama([pause, {"text": "."}])
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "resume", "t1", "--message", "Also docs."])
    out = capsys.readouterr().out
    prompt = json.dumps(requests[0]["messages"][1])
    assert f"The person reviewed this task's PR ({PR}) since the last burst" in prompt
    assert "- brian on src/f.py:12: Off by one." in prompt and "Also docs." in prompt
    assert "1 new comment(s) from review" in out
    assert asked_since[0][:2] == "20" and "T" in asked_since[0]  # the branch's last commit's time: what came after it is new


def test_a_long_review_reaches_the_burst_whole(tmp_path, origin, llama, capsys, monkeypatch):  # noqa: F811
    """A review is one long comment: it is given in full, and what the prompt can't take is in a file the burst
    reads (it can't ask the forge itself), never committed, and gone once a later burst has nothing new."""
    pause = {"tool": "pause_for_review", "args": {"summary": "s", "next_step": "n"}}
    url, _ = llama([pause, {"text": "."}])
    work = tmp_path / "work"
    sh(tmp_path, "clone", "-q", str(origin), str(work))
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "t1", "--task", "Add f."])
    capsys.readouterr()
    monkeypatch.setattr(git, "repo_slug", lambda remote: "github.com/me/proj")
    monkeypatch.setattr(git, "find_pr", lambda repo, head, cfg=None: PR)
    review = "**Findings**\n\n" + "".join(f"{n}. Finding {n}: " + "x" * 600 + "\n" for n in range(1, 9)) + "THE END"
    said = [{"who": "reviewer", "when": "2026-11-21T12:00:00Z", "where": "", "text": review}]
    monkeypatch.setattr(git, "feedback", lambda pr, cfg=None, since="": said)

    def resume():
        url, requests = llama([pause, {"text": "."}])
        cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "resume", "t1"])
        return json.loads(json.dumps(requests[0]["messages"][1]))["content"], capsys.readouterr().out

    prompt, _ = resume()
    text = prompt if isinstance(prompt, str) else json.dumps(prompt)
    assert "THE END" in text and "[...]" not in text and cli.REVIEW_FILE not in text
    assert not (work / cli.REVIEW_FILE).exists()

    said[:] = [{"who": "reviewer", "when": "2026-11-21T12:00:00Z", "where": f"src/f.py:{n}", "text": f"{n}: " + "y" * 5000}
               for n in range(1, 9)]
    prompt, out = resume()
    text = prompt if isinstance(prompt, str) else json.dumps(prompt)
    assert "src/f.py:1:" in text and "src/f.py:8:" not in text and f"more, in {cli.REVIEW_FILE}" in text
    written = (work / cli.REVIEW_FILE).read_text()
    assert all(f"src/f.py:{n}: {n}: " + "y" * 5000 in written for n in range(1, 9))
    assert cli.REVIEW_FILE not in sh(work, "status", "--porcelain", "--untracked-files=all")
    assert f"all of it is in {cli.REVIEW_FILE}" in out

    said.clear()
    prompt, _ = resume()
    assert not (work / cli.REVIEW_FILE).exists()


def test_a_round_that_changed_the_roadmap_waits_for_a_person_after_its_merge(tmp_path, harness, capsys):  # noqa: F811
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "build it", "slug": "t", "auto_continue": True})
    harness.started[0][1].finish(0, state="pr", pr=PR1, metrics={"criteria": {"ticked": 3, "total": 3}},
                                 project={"milestone": "M2", "complete": True, "plan_change": "roadmap", "setup": False})
    record = _settled(runner, "t", "pr")
    assert record["project"] == {"milestone": "M2", "complete": True, "plan_change": "roadmap", "setup": False,
                                 "criteria": {"ticked": 3, "total": 3}}
    harness.prs[PR1] = "merged"
    runner.check_pr("t", 0)
    assert len(harness.started) == 1 and runner.task("t")["state"] == "merged"
    assert "it changed the project's roadmap: the next round waits" in capsys.readouterr().out

    runner.create({"repo": "me/proj", "prompt": "build it", "slug": "u", "auto_continue": True})
    _settled(runner, "t", "merged")
    harness.started[-1][1].finish(0, state="pr", pr="https://github.com/me/proj/pull/9",
                                  project={"milestone": "M2", "complete": True, "plan_change": "item", "setup": False})
    _settled(runner, "u", "pr")
    harness.prs["https://github.com/me/proj/pull/9"] = "merged"
    runner.check_pr("u", 0)
    assert len(harness.started) == 3  # an item change only: on it goes


def test_slack_says_the_milestone_and_a_plan_change():
    assert _project_lines({"project": None}) == []
    assert _project_lines({"project": {"milestone": "M3", "complete": False, "plan_change": None,
                                       "criteria": {"ticked": 2, "total": 5}}}) == [
        "*Milestone:* M3, 2 of 5 Accept items met"]
    lines = _project_lines({"project": {"milestone": "M3", "complete": True, "plan_change": "roadmap"}})
    assert lines[0] == "*Milestone:* M3 — complete" and "It changed the roadmap" in lines[1]
    assert "merging it approves the plan" in _project_lines({"project": {"milestone": "M0", "setup": True}})[0]
