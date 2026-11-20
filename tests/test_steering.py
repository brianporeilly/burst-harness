"""Keeping the record and the docs current (steering.py): the docs style in the prompt, the nudge after many tool
calls with nothing recorded, a burst sent back once to record before it ends, and one docs reminder at finish."""

import json

from harness import cli, steering
from harness.config import HarnessConfig
from harness.exec_target import LocalTarget

from .conftest import git
from .test_agents import llama  # noqa: F401 -- the fixture

SHELL = {"tool": "shell", "args": {"command": "echo working"}}
PAUSE = {"tool": "pause_for_review", "args": {"summary": "half", "next_step": "the rest"}}
FINISH = {"tool": "finish_with_pr", "args": {"title": "Add f", "body": "Adds f."}}


def _run(tmp_path, origin, url, *more):
    work = tmp_path / "work"
    if not work.exists():
        git(tmp_path, "clone", "-q", str(origin), str(work))
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), *more, "start", "t1", "--task", "Go."])
    return work


def _result(out):
    return json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])


def test_the_docs_style_in_the_prompt(tmp_path):
    target = LocalTarget(repo=str(tmp_path))
    assert "Diátaxis" in steering.docs_note(HarnessConfig(), target)
    assert steering.docs_note(HarnessConfig(docs_style="none"), target) == ""
    assert steering.docs_note(HarnessConfig(docs_style="docs/STYLE.md"), target) == ""  # no such file: nothing
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "STYLE.md").write_text("Write in the second person.\n")
    note = steering.docs_note(HarnessConfig(docs_style="docs/STYLE.md"), target)
    assert "this project's style (docs/STYLE.md)" in note and "<docs-style>\nWrite in the second person.\n</docs-style>" in note


def test_the_lead_is_told_the_style(tmp_path, origin, llama, capsys, monkeypatch):  # noqa: F811
    monkeypatch.delenv("HARNESS_DOCS_STYLE")
    url, requests = llama([PAUSE, {"text": "Paused."}])
    _run(tmp_path, origin, url)
    assert "It follows Diátaxis" in requests[0]["messages"][0]["content"]


def test_many_tool_calls_with_nothing_recorded_are_nudged(tmp_path, origin, llama, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setenv("HARNESS_RECORD_NUDGE_CALLS", "3")
    record = {"tool": "record_progress", "args": {"done": "a step"}}
    url, requests = llama([SHELL, SHELL, SHELL, record, SHELL, SHELL, PAUSE, {"text": "Paused."}])
    _run(tmp_path, origin, url)
    told = [i for i, request in enumerate(requests) if "since anything was recorded" in json.dumps(request["messages"][-1])]
    assert told == [3]  # after the third call; the record set the count back, and two more aren't three


def test_a_burst_that_recorded_nothing_is_sent_back_once(tmp_path, origin, llama, capsys):  # noqa: F811
    url, requests = llama([*[SHELL] * steering.MIN_WORK, PAUSE, PAUSE, {"text": "Paused."}])
    _run(tmp_path, origin, url)
    assert "Not yet: this burst recorded nothing" in json.dumps(requests[steering.MIN_WORK + 1]["messages"][-1])
    assert _result(capsys.readouterr().out)["state"] == "paused"  # the second pause went through

    url, requests = llama([*[SHELL] * steering.MIN_WORK, {"tool": "record_progress", "args": {"done": "x"}}, PAUSE,
                           {"text": "Paused."}])
    git(tmp_path / "work", "checkout", "-q", "main")
    git(tmp_path / "work", "branch", "-q", "-D", "harness/t1")
    git(origin, "branch", "-q", "-D", "harness/t1")
    _run(tmp_path, origin, url)
    assert "Not yet" not in json.dumps(requests) and _result(capsys.readouterr().out)["state"] == "paused"


def test_a_change_to_code_without_docs_gets_one_reminder(tmp_path, origin, llama, capsys, monkeypatch):  # noqa: F811
    monkeypatch.delenv("HARNESS_DOCS_STYLE")
    edit = {"tool": "shell", "args": {"command": "mkdir -p src tests && echo 'def f(): pass' > src/f.py "
                                                 "&& echo 'def test_f(): pass' > tests/test_f.py"}}
    url, requests = llama([edit, FINISH, FINISH, {"text": "Done."}])
    _run(tmp_path, origin, url)
    reminded = json.dumps(requests[2]["messages"][-1], ensure_ascii=False)
    assert "Before the PR: the change touches code (src/f.py) but no documentation" in reminded
    assert "tests/test_f.py" not in reminded and "Diátaxis" in reminded
    assert _result(capsys.readouterr().out)["state"] == "pushed_no_pr"  # finishing again went through


def test_docs_changed_or_none_wanted_means_no_reminder(tmp_path, origin, llama, capsys, monkeypatch):  # noqa: F811
    monkeypatch.delenv("HARNESS_DOCS_STYLE")
    edit = {"tool": "shell", "args": {"command": "echo 'def f(): pass' > f.py && echo '# f' >> README.md"}}
    url, requests = llama([edit, FINISH, {"text": "Done."}])
    _run(tmp_path, origin, url)
    assert "Before the PR" not in json.dumps(requests) and _result(capsys.readouterr().out)["state"] == "pushed_no_pr"


def test_claude_code_is_told_the_style_too(tmp_path):
    from harness.claude_code import ClaudeCodeEngine
    from harness.context import HarnessContext

    ctx = HarnessContext(target=LocalTarget(repo=str(tmp_path)), cfg=HarnessConfig(), slug="t", branch="b",
                         state_key="k")
    assert "It follows Diátaxis" in ClaudeCodeEngine(ctx).rules()
