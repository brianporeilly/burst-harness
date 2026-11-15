"""Runners in the orchestrator: saved in Settings, picked by a task (or the default), granted their SSH keys."""

import os
import threading

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from harness import credentials
from harness.config import HarnessConfig
from orchestrator import tasks as tasks_module
from orchestrator.app import create_app
from orchestrator.runners import RunnerStore, RunnersError
from orchestrator.settings import CredentialStore, SettingsError, load_key
from orchestrator.tasks import TasksError

from .test_orchestrator import CHANGE, FakeBackend
from .test_tasks import _runner, _settled, harness  # noqa: F401 -- harness is a fixture

BOX = "podman+ssh://agent@box.example.com/run/user/1000/podman/podman.sock"
VM = "ssh://agent@vm1.example.com"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(credentials, "_stored", {})
    monkeypatch.setenv("HARNESS_RUNNERS", "")  # the store sets it: put back as it was after each test


def _store(tmp_path, configured=()):
    return RunnerStore(tmp_path / "runners.json", HarnessConfig(runners=tuple(configured)))


def test_saved_runners_are_listed_with_the_configurations_and_exported_to_what_runs(tmp_path):
    store = _store(tmp_path, ["big=docker+ssh://big.example.com"])
    store.set("box", {"url": BOX, "default": True}, user="brian")
    store.set("vm1", {"url": VM})
    listing = store.listing()
    assert listing["default"] == "box"
    rows = {r["name"]: r for r in listing["runners"]}
    assert rows["big"]["source"] == "configuration" and rows["big"]["kind"] == "docker"
    assert rows["box"]["kind"] == "podman" and rows["box"]["updated_by"] == "brian"
    assert rows["vm1"]["kind"] == "ssh" and rows["vm1"]["key"] == "VM1_SSH_KEY" and not rows["vm1"]["key_set"]
    assert os.environ["HARNESS_RUNNERS"] == f"big=docker+ssh://big.example.com,box={BOX},vm1={VM}"
    assert HarnessConfig.load(None, {}).runners == store.entries()  # what a job's config, or a burst, reads

    with pytest.raises(RunnersError, match="ssh://"):
        store.set("bad", {"url": "https://x.example.com"})
    with pytest.raises(RunnersError, match="not saved"):
        store.set("nope", {"default": True})
    with pytest.raises(RunnersError, match="configuration"):
        store.remove("big")
    assert store.remove("box") == "gone" and store.default() == ""
    assert "box=" not in os.environ["HARNESS_RUNNERS"]
    assert store.choose(None) == "" and store.choose("vm1") == "vm1"
    with pytest.raises(RunnersError, match="No runner named"):
        store.choose("box")


def test_a_task_runs_on_the_default_runner_or_the_one_it_picks_with_that_runners_key(
        tmp_path, monkeypatch, harness):  # noqa: F811 -- the fixture
    monkeypatch.setenv("BOX_SSH_KEY", "box key")
    monkeypatch.setenv("VM1_SSH_KEY", "vm1 key")
    store = _store(tmp_path)
    store.set("box", {"url": BOX, "default": True})
    store.set("vm1", {"url": VM})
    runner = _runner(tmp_path, runners=store, limit=3)

    made = runner.create({"repo": "me/proj", "prompt": "x", "slug": "one"})
    assert made["runner"] == "box"
    argv, env = harness.argv(), harness.started[-1][1].env
    assert argv[argv.index("--runner") + 1] == "box" and argv[argv.index("--runner") + 2:][:2] == ["--egress", "allowlist"]
    assert env["BOX_SSH_KEY"] == "box key" and "VM1_SSH_KEY" not in env
    assert f"box={BOX}" in env["HARNESS_RUNNERS"]

    runner.create({"repo": "me/proj", "prompt": "x", "slug": "two", "runner": ""})  # where the orchestrator runs
    assert "--runner" not in harness.argv() and "BOX_SSH_KEY" not in harness.started[-1][1].env

    runner.create({"repo": "me/proj", "prompt": "x", "slug": "three", "runner": "vm1"})
    argv = harness.argv()
    assert argv[argv.index("--runner"):][:4] == ["--runner", "vm1", "--egress", "open"]
    assert argv[::-1].index("--egress") == argv[::-1].index("open") + 1  # open is the last word on it
    assert harness.started[-1][1].env["VM1_SSH_KEY"] == "vm1 key"

    with pytest.raises(TasksError, match="No runner named"):
        runner.create({"repo": "me/proj", "prompt": "x", "slug": "four", "runner": "gone"})


def test_a_plain_host_takes_no_question_and_no_network_of_a_gateway(tmp_path, harness):  # noqa: F811
    store = _store(tmp_path)
    store.set("vm1", {"url": VM, "default": True})
    runner = _runner(tmp_path, runners=store, limit=3)
    for asked in ({"egress": "log"}, {"allow": "docs.example.com"}):
        with pytest.raises(TasksError, match="plain host"):
            runner.create({"repo": "me/proj", "prompt": "x", **asked})
    with pytest.raises(TasksError, match="question"):
        runner.create({"prompt": "What is new?", "runner": "vm1"})
    # The default is passed by for a question: it runs where the orchestrator runs.
    made = runner.create({"prompt": "What is new?", "slug": "q"})
    assert made["runner"] is None and "--runner" not in harness.argv()


def test_a_kept_directory_on_a_plain_host_is_carried_on_in_and_released_with_its_key(
        tmp_path, monkeypatch, harness):  # noqa: F811 -- the fixture
    released = []
    monkeypatch.setattr(tasks_module.subprocess, "run", lambda argv, **kw: released.append((argv, kw["env"])))
    monkeypatch.setenv("VM1_SSH_KEY", "vm1 key")
    store = _store(tmp_path)
    store.set("vm1", {"url": VM})
    store.set("box", {"url": BOX})
    runner = _runner(tmp_path, runners=store)
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "t", "runner": "vm1"})

    for refused in ("--runner vm1 --runner-workdir /home/agent",  # not a directory the harness made
                    "--runner vm1 --runner-workdir /etc/t-1a2b3c",  # not under the runner's path
                    "--runner vm1 --runner-workdir /home/agent/burst/../t-1a2b3c",
                    "--runner box --runner-workdir /home/agent/burst/t-1a2b3c",  # not a plain host
                    "--runner vm1 --runner-workdir /home/agent/burst/t-1a2b3c --target local"):
        assert not runner._keepable(refused), refused
    assert runner._keepable("--runner box --target podman --docker-container burst-t-1a2b3c")

    kept = "--runner vm1 --runner-workdir /home/agent/burst/t-1a2b3c"
    harness.started[0][1].finish(1, state="error", kept=True, target=kept)
    assert _settled(runner, "t", "failed")["sandbox"] == kept
    runner.resume("t")
    argv = harness.argv()
    assert argv[argv.index("--runner-workdir") + 1] == "/home/agent/burst/t-1a2b3c"
    harness.started[1][1].finish(0, state="paused", kept=False, target=kept)
    assert _settled(runner, "t", "paused")["sandbox"] is None
    for _ in range(200):
        if released:
            break
        threading.Event().wait(0.02)
    argv, env = released[0]
    assert argv[3:] == ["--runner", "vm1", "--runner-workdir", "/home/agent/burst/t-1a2b3c", "release"]
    assert env["VM1_SSH_KEY"] == "vm1 key" and f"vm1={VM}" in env["HARNESS_RUNNERS"]


def test_a_runners_ssh_key_and_known_hosts_are_credentials_of_several_lines(tmp_path):
    key, _ = load_key(tmp_path)
    store = CredentialStore(tmp_path / "credentials.enc", key)
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\r\nabc\r\ndef\r\n-----END OPENSSH PRIVATE KEY-----\r\n"
    store.set("VM1_SSH_KEY", pem)
    store.set("VM1_SSH_KNOWN_HOSTS", "vm1.example.com ssh-ed25519 AAAA\nvm1.example.com ssh-rsa BBBB")
    assert credentials.get("VM1_SSH_KEY") == pem.replace("\r\n", "\n").strip()
    assert credentials.is_secret("VM1_SSH_KNOWN_HOSTS")  # granted with its runner, not passed to every run
    assert "VM1_SSH_KNOWN_HOSTS" not in credentials.for_run([])
    with pytest.raises(SettingsError, match="one line"):
        store.set("ANTHROPIC_API_KEY", "sk-one\nsk-two")
    row = next(r for r in store.listing() if r["name"] == "VM1_SSH_KEY")
    assert "SSH private key" in row["use"]


def test_the_runners_api(tmp_path):
    key, _ = load_key(tmp_path)
    settings = CredentialStore(tmp_path / "credentials.enc", key)
    store = _store(tmp_path)
    client = TestClient(create_app(FakeBackend(), settings=settings, runners=store), base_url="http://127.0.0.1:8700")
    put = client.put("/api/settings/runners/vm1", json={"url": VM, "default": True}, headers=CHANGE)
    assert put.status_code == 200 and "new tasks run there" in put.json()["message"]
    assert client.put("/api/settings/runners/x", json={"url": "ftp://x"}, headers=CHANGE).status_code == 400
    assert client.put("/api/settings/credentials/VM1_SSH_KNOWN_HOSTS", json={"value": "vm1 ssh-ed25519 AAAA"},
                      headers=CHANGE).status_code == 200
    names = [row["name"] for row in client.get("/api/settings/credentials").json()]
    assert "VM1_SSH_KEY" in names and "VM1_SSH_KNOWN_HOSTS" in names  # offered, set or not
    row = client.get("/api/settings/runners").json()["runners"][0]
    assert row["known_hosts_set"] and not row["key_set"] and "AAAA" not in repr(row)
    assert client.delete("/api/settings/runners/vm1", headers=CHANGE).json()["message"] == "Removed vm1."
    assert client.get("/api/settings/runners").json() == {"runners": [], "default": ""}
