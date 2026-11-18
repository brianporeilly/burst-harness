"""setup_command: run before a burst's model starts, its result and output in the first prompt."""

import json

from harness import cli

from .conftest import git
from .test_agents import llama  # noqa: F401 -- the fixture


def _result(out):
    return json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])


def _start(tmp_path, origin, url, monkeypatch, command, *more):
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    monkeypatch.setenv("HARNESS_SETUP_COMMAND", command)
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), *more, "start", "t1", "--task", "Go."])
    return work


def test_setup_runs_before_the_model_and_its_output_is_in_the_first_prompt(tmp_path, origin, llama, capsys,  # noqa: F811
                                                                         monkeypatch):
    url, requests = llama([{"text": "done"}])
    work = _start(tmp_path, origin, url, monkeypatch, "echo installed 3 packages; touch .ready")
    result = _result(capsys.readouterr().out)
    first = json.dumps(requests[0]["messages"])
    assert "Go." in first and "the harness ran this repo's setup" in first and "installed 3 packages" in first
    assert "exited 0" in first and "It failed" not in first
    assert (work / ".ready").exists()
    assert result["metrics"]["setup"]["exit"] == 0


def test_a_failed_setup_is_the_models_to_deal_with(tmp_path, origin, llama, capsys, monkeypatch):  # noqa: F811
    url, requests = llama([{"text": "done"}])
    _start(tmp_path, origin, url, monkeypatch, "echo 'no lockfile' >&2; exit 2")
    result = _result(capsys.readouterr().out)
    first = json.dumps(requests[0]["messages"])
    assert "exited 2" in first and "It failed" in first and "no lockfile" in first
    assert len(requests) >= 1 and result["metrics"]["setup"]["exit"] == 2


def test_no_setup_command_runs_nothing(tmp_path, origin, llama, capsys):  # noqa: F811
    url, requests = llama([{"text": "done"}])
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "t1", "--task", "Go."])
    assert "<setup-output>" not in json.dumps(requests[0]["messages"]) and _result(capsys.readouterr().out)["metrics"]["setup"] is None
