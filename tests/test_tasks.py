"""The orchestrator's tasks: a repo and a prompt, run as bursts of `harness run`, queued, continued, cancelled."""

import json
import signal
import threading
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from harness import git
from harness.config import HarnessConfig
from orchestrator import tasks as tasks_module
from orchestrator.app import create_app
from orchestrator.tasks import TaskRunner, TasksError, repo_url

from .test_orchestrator import CHANGE, FakeBackend


class Harness:
    """Stands in for `harness run` subprocesses: each waits until the test ends it, with a result or none."""

    def __init__(self):
        self.started = []  # (argv, process)

    def __call__(self, argv, **kwargs):
        process = FakeProcess(argv, kwargs["stdout"].name)
        process.env = kwargs["env"]
        self.started.append((argv, process))
        return process

    def argv(self, index=-1):
        return self.started[index][0]


class FakeProcess:
    count = 1000

    def __init__(self, argv, output):
        FakeProcess.count += 1
        self.pid, self.argv, self.output = FakeProcess.count, argv, output
        self.done, self.code = threading.Event(), None

    def finish(self, code=0, **result):
        with open(self.output, "a") as handle:
            handle.write("working...\n")
            if result:
                handle.write("HARNESS_RESULT " + json.dumps(result) + "\n")
        self.code = code
        self.done.set()

    def wait(self):
        self.done.wait(10)
        return self.code

    def poll(self):
        return self.code


@pytest.fixture
def harness(monkeypatch):
    fake = Harness()
    monkeypatch.setattr(tasks_module.subprocess, "Popen", fake)
    monkeypatch.setattr(tasks_module.git, "default_branch", lambda url: "trunk")
    return fake


def _runner(tmp_path, target="local", **options):
    base = replace(HarnessConfig.load(None, {}), state_dir=str(tmp_path / "state"), target=target, egress="open",
                   engine="strands", branch_prefix="harness/")
    return TaskRunner(base, **options)


def _settled(runner, slug, state):
    """Wait for a task to be in that state, with no burst of it running unless that is the state."""
    for _ in range(200):
        if runner.task(slug)["state"] == state and (state == "running") == (slug in runner.running):
            return runner.task(slug)
        threading.Event().wait(0.02)
    raise AssertionError(f"{slug} is {runner.task(slug)['state']}, not {state}")


def test_repos_are_owner_slash_name_or_an_https_url():
    assert repo_url("me/proj") == "https://github.com/me/proj.git"
    assert repo_url(" https://git.lan/me/proj.git/ ") == "https://git.lan/me/proj.git"
    for bad in ("", "proj", "git@github.com:me/proj.git", "http://x/y", "https://x/../y", "me/proj --upload-pack=x"):
        with pytest.raises(TasksError):
            repo_url(bad)


def test_a_task_runs_on_docker_when_that_is_the_target(tmp_path, harness):
    _runner(tmp_path, target="docker").create({"repo": "me/proj", "prompt": "x"})
    argv = harness.argv()
    assert ["--target", "docker", "--egress", "allowlist"] == argv[argv.index("--target"):argv.index("--target") + 4]


def test_a_task_is_checked_then_run_as_harness_run_in_a_container(tmp_path, harness):
    runner = _runner(tmp_path)
    with pytest.raises(TasksError, match="prompt"):
        runner.create({"repo": "me/proj"})
    with pytest.raises(TasksError, match="slug"):
        runner.create({"repo": "me/proj", "prompt": "x", "slug": "Bad Slug"})
    with pytest.raises(TasksError, match="engine"):
        runner.create({"repo": "me/proj", "prompt": "x", "engine": "vim"})
    made = runner.create({"repo": "me/proj", "prompt": "Fix the flaky test, please!", "base_branch": "dev",
                          "draft": True, "model": "claude-opus-5-5"}, user="brian")
    assert made["slug"].startswith("fix-the-flaky-test-please-") and made["state"] == "running"
    assert made["branch"] == "harness/" + made["slug"] and made["created_by"] == "brian"
    argv = harness.argv()
    assert argv[argv.index("--repo-url") + 1] == "https://github.com/me/proj.git"
    assert ["--target", "podman", "--egress", "allowlist"] == argv[argv.index("--target"):argv.index("--target") + 4]
    assert "--draft-pr" in argv and argv[argv.index("--base-branch") + 1] == "dev"
    assert harness.started[0][1].env["HARNESS_MODEL_ID"] == "claude-opus-5-5"
    assert argv[-4:] == ["run", made["slug"], "--task", "Fix the flaky test, please!"]
    with pytest.raises(TasksError, match="already"):
        runner.create({"repo": "me/proj", "prompt": "again", "slug": made["slug"]})
    harness.started[0][1].finish(0, state="pr", pr="https://github.com/me/proj/pull/7")
    done = _settled(runner, made["slug"], "pr")
    assert done["pr"] == "https://github.com/me/proj/pull/7" and done["bursts"][0]["exit"] == 0
    assert "working..." in done["output"] and "=== burst 1" in done["output"]


def test_without_a_base_branch_a_task_is_based_on_the_repos_default(tmp_path, harness, monkeypatch):
    runner = _runner(tmp_path)
    made = runner.create({"repo": "me/proj", "prompt": "x", "slug": "t"})
    assert made["base_branch"] == "trunk" and harness.argv()[harness.argv().index("--base-branch") + 1] == "trunk"

    def unreadable(url):
        raise git.GitError(f"cannot read {url}: fatal: could not read Username")

    monkeypatch.setattr(tasks_module.git, "default_branch", unreadable)
    with pytest.raises(TasksError, match="cannot read https://github.com/me/secret.git"):
        runner.create({"repo": "me/secret", "prompt": "x", "slug": "u"})
    assert len(harness.started) == 1


def test_a_repos_default_branch_is_asked_of_the_remote(monkeypatch):
    calls = []

    class Done:
        returncode, stderr = 0, ""
        stdout = "ref: refs/heads/master\tHEAD\n7fd1a60b01f91b314f59955a4e4d4e80d8edf11d\tHEAD\n"

    def run(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return Done()

    monkeypatch.setattr(git.subprocess, "run", run)
    monkeypatch.setenv("GH_TOKEN", "tok")
    assert git.default_branch("https://github.com/octocat/Hello-World.git") == "master"
    (argv, env), = calls
    assert argv[:3] == ["git", "-c", "credential.helper="] and argv[-3:] == ["--symref", "https://github.com/octocat/Hello-World.git", "HEAD"]
    assert any(a.startswith("http.extraHeader=Authorization: Basic ") for a in argv) and env["GIT_TERMINAL_PROMPT"] == "0"
    Done.returncode, Done.stderr = 128, "fatal: could not read Username for 'https://github.com'"
    with pytest.raises(git.GitError, match="cannot read"):
        git.default_branch("https://github.com/me/secret.git")


def test_a_burst_gets_the_git_token_and_model_key_not_every_credential(tmp_path, harness, monkeypatch):
    for name in ("GH_TOKEN", "ANTHROPIC_API_KEY", "KUBE_TOKEN", "SLACK_BOT_TOKEN"):
        monkeypatch.setenv(name, name.lower())
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "t"})
    env = harness.started[0][1].env
    assert env["GH_TOKEN"] == "gh_token" and env["ANTHROPIC_API_KEY"] == "anthropic_api_key"
    assert "KUBE_TOKEN" not in env and "SLACK_BOT_TOKEN" not in env and env["PYTHONUNBUFFERED"] == "1"


def test_a_task_on_a_plugin_provider_is_granted_its_credentials(tmp_path, harness, monkeypatch):
    from .fake_provider import PROVIDER

    monkeypatch.setenv("EXAMPLE_CLIENT_SECRET", "s3cret")
    monkeypatch.setattr(tasks_module, "check_model", lambda *args: None)
    runner = _runner(tmp_path, limit=2)  # Anthropic by default
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "plain"})
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "plugin", "provider": PROVIDER})
    assert "EXAMPLE_CLIENT_SECRET" not in harness.started[0][1].env
    assert harness.started[1][1].env["EXAMPLE_CLIENT_SECRET"] == "s3cret"


def test_a_task_can_use_an_installed_provider_plugin_but_a_session_cannot(tmp_path, harness, monkeypatch):
    from types import SimpleNamespace

    from harness import models
    from orchestrator.backend import PodmanBackend, SessionSpec, SpecError

    from . import fake_provider

    installed = [SimpleNamespace(name="example-gateway", load=lambda: fake_provider.plain)]
    monkeypatch.setattr(models, "entry_points", lambda group, name=None: [
        ep for ep in installed if name in (None, ep.name)])
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "t", "provider": "example-gateway", "model": "big"})
    env = harness.started[0][1].env
    assert (env["HARNESS_PROVIDER"], env["HARNESS_MODEL_ID"], env["HARNESS_BASE_URL"]) == ("example-gateway", "big", "")
    info = PodmanBackend(replace(runner.base)).info()
    assert "example-gateway" in info["task_providers"] and "anthropic" in info["task_providers"]
    assert "example-gateway" not in info["providers"]  # a session's opencode knows only its own
    with pytest.raises(SpecError, match="provider is one of"):
        SessionSpec.parse({"name": "box", "provider": "example-gateway"})


def test_claude_code_keeps_a_default_provider_that_gives_it_an_endpoint(tmp_path, harness):
    from orchestrator.backend import PodmanBackend

    runner = _runner(tmp_path, limit=2)
    runner.base = replace(runner.base, provider="llamacpp", base_url="http://typhon:8080")
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "plain", "engine": "claude-code"})
    assert harness.started[0][1].env["HARNESS_PROVIDER"] == "anthropic"  # not the default's llama.cpp
    runner.base = replace(runner.base, provider="tests.fake_provider:gateway")
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "gateway", "engine": "claude-code"})
    assert "HARNESS_PROVIDER" not in harness.started[1][1].env  # the orchestrator's own: the plugin
    assert PodmanBackend(runner.base).info()["defaults"]["claude_endpoint"] is True


def test_a_task_can_choose_its_provider_and_model(tmp_path, harness, monkeypatch):
    runner = _runner(tmp_path, limit=10)
    runner.base = replace(runner.base, provider="llamacpp", base_url="http://typhon:8080", model_id="raon",
                          context_window_limit=65536)
    monkeypatch.setenv("HARNESS_BASE_URL", "http://typhon:8080")  # as a deployment sets it
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "same"})
    env = harness.started[-1][1].env  # nothing chosen: the orchestrator's own settings
    assert env["HARNESS_BASE_URL"] == "http://typhon:8080"
    assert not {"HARNESS_PROVIDER", "HARNESS_MODEL_ID", "HARNESS_CONTEXT_WINDOW_LIMIT", "HARNESS_ROUTER"} & set(env)
    # The burst was routed to a profile (harness/agents.py): the task shows what worked on it.
    harness.started[-1][1].finish(0, state="paused", model="anthropic claude-haiku-5-5", base_url="", agent="quick",
                                  delegated={"coder": {"calls": 2, "input": 10, "output": 5}})
    routed = _settled(runner, "same", "paused")
    assert routed["uses"] == {"provider": "anthropic", "model": "claude-haiku-5-5", "base_url": "", "agent": "quick"}
    assert routed["bursts"][-1]["delegated"]["coder"]["calls"] == 2
    # Anthropic: none of llama.cpp's URL, label or window goes with it.
    made = runner.create({"repo": "me/proj", "prompt": "x", "slug": "claude", "provider": "anthropic"})
    env = harness.started[-1][1].env
    assert (env["HARNESS_PROVIDER"], env["HARNESS_BASE_URL"], env["HARNESS_MODEL_ID"]) == ("anthropic", "", "claude-opus-5-5")
    assert env["HARNESS_CONTEXT_WINDOW_LIMIT"] == "1000000" and made["provider"] == "anthropic"
    assert env["HARNESS_ROUTER"] == ""  # given its model: not routed
    assert made["uses"] == {"provider": "anthropic", "model": "claude-opus-5-5", "base_url": ""}
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "other", "provider": "openai",
                   "base_url": "http://other:8000/v1", "model": "qwen3", "context_window": 32768})
    env = harness.started[-1][1].env
    assert (env["HARNESS_BASE_URL"], env["HARNESS_MODEL_ID"], env["HARNESS_CONTEXT_WINDOW_LIMIT"]) == (
        "http://other:8000/v1", "qwen3", "32768")
    # Claude Code is Anthropic's, whatever the orchestrator's provider.
    made = runner.create({"repo": "me/proj", "prompt": "x", "slug": "cc", "engine": "claude-code"})
    assert made["provider"] == "anthropic" and harness.started[-1][1].env["HARNESS_BASE_URL"] == ""
    for raw, message in [({"provider": "openai"}, "needs a base URL"), ({"provider": "vertex"}, "provider is one of"),
                         ({"base_url": "file:///etc"}, "not a base URL"), ({"model": "a b"}, "not a model id"),
                         ({"context_window": "lots"}, "number of tokens"),
                         ({"engine": "claude-code", "provider": "llamacpp"}, "always uses Anthropic")]:
        with pytest.raises(TasksError, match=message):
            runner.create({"repo": "me/proj", "prompt": "x", "slug": "bad", **raw})


def test_on_a_cluster_the_target_is_the_orchestrators_own(tmp_path, harness):
    runner = _runner(tmp_path, claim_ttl=7200)
    runner.base = replace(runner.base, target="agent-sandbox")
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "one"})
    argv = harness.argv()
    assert "--target" not in argv and argv[argv.index("--claim-ttl") + 1] == "7200"


def test_tasks_queue_beyond_the_limit_and_start_in_order(tmp_path, harness):
    runner = _runner(tmp_path, limit=1)
    runner.create({"repo": "me/proj", "prompt": "first", "slug": "a"})
    second = runner.create({"repo": "me/proj", "prompt": "second", "slug": "b"})
    runner.create({"repo": "me/proj", "prompt": "third", "slug": "c"})
    assert second["state"] == "queued" and len(harness.started) == 1
    harness.started[0][1].finish(1)  # no result line: no terminal state
    assert _settled(runner, "a", "failed")["bursts"][0]["state"] == "failed"
    assert _settled(runner, "b", "running") and runner.task("c")["state"] == "queued"
    assert harness.argv()[-1] == "second"


def test_continuing_a_paused_task_is_another_burst_with_the_reply(tmp_path, harness):
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "do it", "slug": "t"})
    with pytest.raises(TasksError, match="wait"):
        runner.resume("t", "now")
    harness.started[0][1].finish(0, state="paused")
    _settled(runner, "t", "paused")
    assert runner.resume("t", "  Use the v2 API.  ")["state"] == "running"
    assert harness.argv()[-2:] == ["--message", "Use the v2 API."]
    harness.started[1][1].finish(0, state="pr", pr="https://github.com/me/proj/pull/8")
    done = _settled(runner, "t", "pr")
    assert [b["message"] for b in done["bursts"]] == [None, "Use the v2 API."] and done["message"] is None
    runner.resume("t")  # carry on, with no reply: no --message
    assert "--message" not in harness.argv()


def _answer(runner, process, answer, **outcome):
    """End a question's burst as `harness ask` would: its answer kept with its run, and HARNESS_JOB said."""
    run = process.env["HARNESS_JOB_RUN_ID"]
    slug = process.argv[process.argv.index("ask") + 1]
    if answer is not None:
        directory = runner.base.resolve_state_dir(f"asks/{slug}/runs/{run}")
        directory.mkdir(parents=True)
        (directory / "report.md").write_text(answer)
    with open(process.output, "a") as handle:
        handle.write("HARNESS_JOB " + json.dumps({"state": "reported" if answer is not None else "failed",
                                                  "run": run, **outcome}) + "\n")
    process.code = 0 if answer is not None else 1
    process.done.set()


def test_a_task_with_no_repo_is_a_question_answered_then_followed_up(tmp_path, harness):
    runner = _runner(tmp_path)
    made = runner.create({"prompt": "Ten uses for a brick?", "slug": "bricks", "base_branch": "x", "draft": True})
    assert (made["repo"], made["branch"], made["base_branch"], made["draft"]) == (None, None, "", False)
    argv, process = harness.started[0]
    assert argv[-4:] == ["ask", "bricks", "--task", "Ten uses for a brick?"] and "--repo-url" not in argv
    assert ["--target", "podman", "--egress", "allowlist"] == argv[argv.index("--target"):argv.index("--target") + 4]
    _answer(runner, process, "1. A doorstop.", agent="local-quick", model="llamacpp lfm3", reason=None)
    done = _settled(runner, "bricks", "answered")
    assert done["bursts"][0]["answer"] == "1. A doorstop." and done["uses"]["agent"] == "local-quick"

    with pytest.raises(TasksError, match="say what to ask next"):
        runner.resume("bricks", " ")
    runner.resume("bricks", "Which is safest?")
    argv, process = harness.started[1]
    assert argv[-2:] == ["--message", "Which is safest?"]
    assert process.env["HARNESS_AGENT"] == "local-quick" and process.env["HARNESS_ROUTER"] == ""  # the same profile
    _answer(runner, process, None, reason="the model ended without calling finish_report")
    failed = _settled(runner, "bricks", "failed")
    assert failed["bursts"][1]["reason"] == "the model ended without calling finish_report"
    runner.resume("bricks")  # asked again: `harness ask` knows the last one gave no answer
    assert "--message" not in harness.argv()
    _answer(runner, harness.started[2][1], "The doorstop.")
    _settled(runner, "bricks", "answered")

    asked = runner.base.resolve_state_dir("asks/bricks")
    assert asked.is_dir()
    runner.remove("bricks")
    assert not asked.exists()  # a new question of that name starts afresh
    asked.mkdir(parents=True)
    with pytest.raises(TasksError, match="asked before"):
        runner.create({"prompt": "again", "slug": "bricks"})


def test_cancelling_a_queued_task_or_stopping_a_running_one(tmp_path, harness, monkeypatch):
    signals = []
    monkeypatch.setattr(tasks_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(tasks_module, "CANCEL_GRACE", 0.01)
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "a"})
    runner.create({"repo": "me/proj", "prompt": "y", "slug": "b"})
    assert runner.cancel("b")["state"] == "cancelled"
    with pytest.raises(TasksError, match="cancel it first"):
        runner.remove("a")
    process = harness.started[0][1]
    runner.cancel("a")
    assert signals[0] == (process.pid, signal.SIGINT)
    threading.Event().wait(0.1)
    assert (process.pid, signal.SIGKILL) in signals  # it had not ended within the grace
    process.finish(130)
    _settled(runner, "a", "cancelled")
    with pytest.raises(TasksError, match="not running"):
        runner.cancel("a")
    runner.remove("a")
    with pytest.raises(TasksError):
        runner.task("a")
    assert len(harness.started) == 1  # b was never started


def test_a_kept_sandbox_is_where_the_next_burst_carries_on_until_the_work_is_pushed(tmp_path, harness, monkeypatch):
    released = []
    monkeypatch.setattr(tasks_module.subprocess, "run", lambda argv, **kwargs: released.append(argv))
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "t"})
    kept = "--target podman --docker-container burst-t-1a2b3c"
    harness.started[0][1].finish(1, state="error", kept=True, target=kept)
    assert _settled(runner, "t", "failed")["sandbox"] == kept
    runner.resume("t")
    argv = harness.argv()
    assert argv[argv.index("--docker-container") + 1] == "burst-t-1a2b3c" and argv.index("--docker-container") < argv.index("run")
    assert argv[::-1].index("--target") < argv[::-1].index("--egress")  # the kept target's --target comes last
    harness.started[1][1].finish(1, state="error", kept=False, target=kept)  # attached: the harness keeps nothing itself
    assert _settled(runner, "t", "failed")["sandbox"] == kept and not released
    runner.resume("t")
    harness.started[2][1].finish(0, state="paused", kept=False, target=kept)
    assert _settled(runner, "t", "paused")["sandbox"] is None
    for _ in range(200):  # the release comes just after the state is written, outside the lock
        if released:
            break
        threading.Event().wait(0.02)
    assert released == [[*released[0][:3], "--target", "podman", "--docker-container", "burst-t-1a2b3c", "release"]]
    assert "releasing the kept sandbox" in runner.task("t")["output"]


def test_a_burst_stopped_by_the_output_limit_keeps_its_sandbox_for_the_next(tmp_path, harness):
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "t"})
    hint = "--target podman --docker-container burst-t-abc123"
    harness.started[0][1].finish(1, state="max_tokens", target=hint, kept=True)
    done = _settled(runner, "t", "max_tokens")
    assert done["sandbox"] == hint
    runner.resume("t")
    assert hint in " ".join(harness.argv())


def test_only_the_harness_shapes_of_a_kept_sandbox_are_taken(tmp_path, harness, monkeypatch):
    released = []
    monkeypatch.setattr(tasks_module.subprocess, "run", lambda argv, **kwargs: released.append(argv))
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "t"})
    harness.started[0][1].finish(1, state="error", kept=True, target="--target local --repo /home")
    assert _settled(runner, "t", "failed")["sandbox"] is None
    runner.resume("t")
    claim = "--target agent-sandbox --claim harness-t-x9"
    harness.started[1][1].finish(1, state="error", kept=True, target=claim)
    assert _settled(runner, "t", "failed")["sandbox"] == claim
    runner.remove("t")  # removing the task releases what it kept
    assert released[0][-5:] == ["--target", "agent-sandbox", "--claim", "harness-t-x9", "release"]


def test_a_burst_cut_short_by_a_restart_is_queued_again_a_few_times(tmp_path, harness):
    first = _runner(tmp_path)
    first.create({"repo": "me/proj", "prompt": "x", "slug": "t"})
    first.create({"repo": "me/proj", "prompt": "y", "slug": "u"})
    first.cancel("u")
    for restart in range(1, tasks_module.RESTARTS + 2):
        later = _runner(tmp_path)  # another orchestrator, after this one stopped
        lines = later.recover()
        record = later.task("t")
        if restart <= tasks_module.RESTARTS:
            assert record["state"] == "queued" and record["restarts"] == restart and "queued again" in lines[0]
            later.dispatch()
            assert later.task("t")["state"] == "running"
        else:
            assert record["state"] == "interrupted" and "left interrupted" in lines[0]
    assert [b["state"] for b in record["bursts"]] == ["interrupted"] * (tasks_module.RESTARTS + 1)
    assert later.task("u")["state"] == "cancelled"


def test_the_tasks_api(tmp_path, harness):
    runner = _runner(tmp_path)
    app = create_app(FakeBackend(), tasks=runner, hosts=("orchestrator.lan",), user_header="Remote-User")
    client = TestClient(app, base_url="http://orchestrator.lan")
    signed_in = {**CHANGE, "Remote-User": "brian"}
    assert client.post("/api/tasks", json={"repo": "me/proj", "prompt": "x"}, headers={"Remote-User": "b"}).status_code == 403
    made = client.post("/api/tasks", json={"repo": "me/proj", "prompt": "Add a README", "slug": "readme"},
                       headers=signed_in)
    assert made.status_code == 202 and made.json()["created_by"] == "brian"
    listed = client.get("/api/tasks", headers=signed_in).json()
    assert listed["on"] and [t["slug"] for t in listed["tasks"]] == ["readme"]
    bad = client.post("/api/tasks", json={"repo": "nope", "prompt": "x"}, headers=signed_in)
    assert bad.status_code == 400 and "not a repo" in bad.json()["error"]
    assert client.post("/api/tasks/readme/continue", headers=signed_in).status_code == 400  # still running
    harness.started[0][1].finish(0, state="paused")
    _settled(runner, "readme", "paused")
    assert client.post("/api/tasks/readme/continue", headers=signed_in).status_code == 202
    harness.started[1][1].finish(0, state="abandoned")
    _settled(runner, "readme", "abandoned")
    assert client.get("/api/tasks/readme", headers=signed_in).json()["output"].count("=== burst") == 2
    assert client.delete("/api/tasks/readme", headers=signed_in).status_code == 200
    assert client.get("/api/tasks/readme", headers=signed_in).status_code == 400
    assert client.get("/api/tasks/..%2Fetc", headers=signed_in).status_code in (400, 404)


def test_without_tasks_the_api_says_so():
    client = TestClient(create_app(FakeBackend()), base_url="http://127.0.0.1:8700")
    assert client.get("/api/tasks").json() == {"on": False, "tasks": []}
    assert client.post("/api/tasks", json={}, headers=CHANGE).status_code == 400


def test_a_pr_that_already_exists_is_the_tasks_pr(monkeypatch):
    class Done:
        returncode, stdout = 1, ""
        stderr = 'a pull request for branch "harness/t" into branch "main" already exists:\nhttps://github.com/me/proj/pull/9\n'

    monkeypatch.setattr(git.subprocess, "run", lambda *a, **k: Done())
    assert git.open_pr(repo="me/proj", head="harness/t", title="t", body="b", base="main", draft=False) \
        == "https://github.com/me/proj/pull/9"
    Done.stderr = "HTTP 422: Validation Failed"
    with pytest.raises(git.GitError):
        git.open_pr(repo="me/proj", head="harness/t", title="t", body="b", base="main", draft=False)
