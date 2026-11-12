"""Job sources (orchestrator/sources.py): git repos of jobs, synced into the orchestrator's state."""

import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from orchestrator import sources as sources_module
from orchestrator.app import create_app
from orchestrator.jobs import JobRunner
from orchestrator.sources import JobSources, Source, SourcesError, parse

from .test_orchestrator import CHANGE, FakeBackend, _jobs_dir


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path, jobs=("digest",), folder="jobs", branch="main"):
    repo = tmp_path / "jobs-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    for name in jobs:
        _add(repo, f"{folder}/{name}" if folder else name)
    (repo / "README.md").write_text("my jobs\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "jobs")
    return repo


def _add(repo, where, prompt="do it"):
    path = repo / where
    path.mkdir(parents=True, exist_ok=True)
    (path / "job.toml").write_text("")
    (path / "prompt.md").write_text(prompt)


def _commit(repo, message="more"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _sources(tmp_path, *configured, **kwargs):
    return JobSources(tmp_path / "state" / "sources", tmp_path / "state" / "job-sources.json", list(configured), **kwargs)


@pytest.mark.parametrize("value, expected", [
    ("https://github.com/you/jobs.git", [Source("jobs", "https://github.com/you/jobs.git")]),
    ("work=https://git.example.com/team/Ops_Jobs.git#release:jobs/daily\n  https://github.com/you/My.Jobs",
     [Source("work", "https://git.example.com/team/Ops_Jobs.git", "release", "jobs/daily"),
      Source("my-jobs", "https://github.com/you/My.Jobs")]),
    ("", []),
])
def test_the_configuration_names_repos_with_a_branch_and_folder(value, expected):
    assert parse(value) == expected


@pytest.mark.parametrize("value, error", [
    ("http://github.com/you/jobs.git", "https://"),
    ("https://you:secret@github.com/you/jobs.git", "no user or password"),
    ("https://github.com/you/jobs.git#main:../etc", "path inside the repo"),
    ("https://github.com/you/jobs.git#-x", "not a branch"),
    ("https://a.example.com/jobs https://b.example.com/jobs", "two sources are named jobs"),
    ("Bad_Name=https://a.example.com/jobs", "https://"),  # not a name, so taken as the URL
])
def test_the_configuration_s_mistakes_are_refused(value, error):
    with pytest.raises(SourcesError, match=error):
        parse(value)


def test_a_repo_is_synced_one_commit_at_a_time_and_runs_say_which(tmp_path):
    repo = _repo(tmp_path)
    store = _sources(tmp_path, Source("mine", str(repo), path="jobs"))
    first = _git(repo, "rev-parse", "HEAD")
    assert store.sync() == {"mine": {"checked": store.status["mine"]["checked"],
                                     "synced": store.status["mine"]["synced"], "error": ""}}
    assert store.commit("mine") == first
    (source, folder), = store.directories()
    assert folder == store.root / "mine" / "trees" / first / "jobs" and (folder / "digest" / "prompt.md").is_file()
    assert store.origin(folder / "digest") == ("mine", first)
    assert store.origin(tmp_path) is None
    assert not (folder.parent / ".git").exists()  # the files at that commit, not a checkout

    # A new commit: unpacked beside the last, which is kept for a run copying it; the one before that goes.
    _add(repo, "jobs/watcher")
    second = _commit(repo)
    store.sync("mine")
    assert store.commit("mine") == second
    (repo / "jobs" / "watcher" / "prompt.md").write_text("changed")
    third = _commit(repo)
    store.sync("mine")
    assert sorted(p.name for p in (store.root / "mine" / "trees").iterdir()) == sorted([second, third])
    assert store.listing()["sources"][0]["jobs"] == ["digest", "watcher"]
    # Nothing new: nothing changes.
    store.sync("mine")
    assert store.commit("mine") == third


def test_the_default_branch_is_used_unless_one_is_named(tmp_path):
    repo = _repo(tmp_path, branch="trunk")
    _git(repo, "switch", "-q", "-c", "next")
    _add(repo, "jobs/only-on-next")
    nxt = _commit(repo)
    _git(repo, "switch", "-q", "trunk")
    store = _sources(tmp_path, Source("default", str(repo), path="jobs"), Source("next", str(repo), "next", "jobs"))
    store.sync()
    assert store.commit("next") == nxt and store.commit("default") == _git(repo, "rev-parse", "trunk")


@pytest.mark.parametrize("make, error", [
    (lambda repo: os.symlink("/etc/passwd", repo / "jobs" / "digest" / "passwd"), "could not be unpacked"),
    (lambda repo: None, "not a folder"),
])
def test_what_is_wrong_with_a_repo_is_said_and_its_jobs_stay_as_they_were(tmp_path, make, error):
    repo = _repo(tmp_path)
    store = _sources(tmp_path, Source("mine", str(repo), path="jobs" if "unpacked" in error else "nope"))
    if "unpacked" in error:
        make(repo)
        _commit(repo)
    status = store.sync()["mine"]
    assert error in status["error"] and "synced" not in status and store.commit("mine") == ""
    missing = _sources(tmp_path, Source("gone", str(tmp_path / "no-such-repo")))
    assert missing.sync()["gone"]["error"]


def test_the_token_goes_to_git_in_its_environment_never_its_arguments(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 128, b"", b"fatal: Authentication failed for 'https://...'")

    monkeypatch.setattr(sources_module.subprocess, "run", fake_run)
    monkeypatch.setenv("JOBS_GIT_TOKEN", "s3cret-token")
    store = _sources(tmp_path, Source("mine", "https://git.example.com/you/jobs.git", "main"))
    error = store.sync()["mine"]["error"]
    assert "is JOBS_GIT_TOKEN set" in error and "s3cret" not in error
    argv, env = calls[-1]
    assert not any("s3cret" in a or "Authorization" in a for a in argv)
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader" and env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # A local repo is never sent the token.
    calls.clear()
    _sources(tmp_path, Source("local", str(tmp_path), "main")).sync()
    assert all("GIT_CONFIG_KEY_0" not in env for _, env in calls)


def test_repos_saved_in_settings_are_synced_and_their_jobs_run_from_their_commit(tmp_path, monkeypatch):
    from orchestrator import launch as launch_module

    root, job = _jobs_dir(tmp_path)  # digest and watcher, in a directory
    repo = _repo(tmp_path, jobs=("watcher", "report"))
    configured = Source("team", "https://git.example.com/team/jobs.git", "main")
    store = _sources(tmp_path, configured)
    runner = JobRunner([root], sources=store)
    test = TestClient(create_app(FakeBackend(), runner), base_url="http://127.0.0.1:8700")

    saved = test.put("/api/settings/job-sources/mine", headers=CHANGE, json={"url": str(repo), "path": "jobs"})
    head = _git(repo, "rev-parse", "HEAD")
    assert saved.status_code == 200 and saved.json()["message"] == f"mine is at {head[:12]}: report."
    listed = {j["name"]: j for j in test.get("/api/jobs").json()["jobs"]}
    assert sorted(listed) == ["digest", "report", "watcher"]
    assert listed["report"]["source"] == {"name": "mine", "commit": head}
    assert "source" not in listed["watcher"]  # the directory's watcher is found first...
    assert listed["watcher"]["also_in"] == [f"source mine at {head[:12]}"]  # ...and the repo's is left out
    rows = {r["name"]: r for r in test.get("/api/settings/job-sources").json()["sources"]}
    assert rows["mine"]["jobs"] == ["report"] and rows["mine"]["left_out"] == ["watcher"]
    assert rows["mine"]["origin"] == "settings" and rows["mine"]["token"] == "JOBS_GIT_TOKEN"
    assert rows["team"]["origin"] == "configuration" and rows["team"]["commit"] == ""

    # A run copies the job at that commit into its record, and says where it came from.
    started = []

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs["env"]))

        def wait(self):
            pass

    with monkeypatch.context() as patched:  # Popen is subprocess's, which syncing uses too
        patched.setattr(launch_module.subprocess, "Popen", FakeProcess)
        run_id = test.post("/api/jobs/report/run", headers=CHANGE).json()["run"]
    (argv, env), = started
    cfg = job.config(job.load(runner.discover()["report"]))
    assert argv[-1] == str(job.runs_dir(cfg, "report") / run_id / "report")
    assert (job.runs_dir(cfg, "report") / run_id / "report" / "prompt.md").read_text() == "do it"
    assert env[job.SOURCE_ENV] == f"mine@{head}"

    # The configuration's cannot be changed or removed here; a saved one can.
    assert "configuration" in test.put("/api/settings/job-sources/team", headers=CHANGE,
                                       json={"url": str(repo)}).json()["error"]
    assert test.delete("/api/settings/job-sources/team", headers=CHANGE).status_code == 400
    assert test.post("/api/settings/job-sources/mine/sync", headers=CHANGE).status_code == 200
    assert test.delete("/api/settings/job-sources/mine", headers=CHANGE).status_code == 200
    assert "report" not in [j["name"] for j in test.get("/api/jobs").json()["jobs"]]
    assert not (store.root / "mine").exists()
    assert test.put("/api/settings/job-sources/bad", headers=CHANGE,
                    json={"url": "http://example.com/x"}).status_code == 400


def test_a_run_records_the_commit_its_job_came_from(tmp_path, monkeypatch, capsys):
    from harness import job

    monkeypatch.setenv(job.SOURCE_ENV, "mine@" + "a" * 40)
    path = tmp_path / "digest"
    path.mkdir()
    (path / "job.toml").write_text('engine = "claude-code"\n')
    (path / "prompt.md").write_text("x")
    spec = job.load(path)
    cfg = job.config(spec)

    def broken(cfg, spec):
        raise job.containers.ContainerError("no image")

    job.run(spec, cfg, acquire=broken)
    assert job.list_runs(cfg, "digest")[0]["source"] == {"name": "mine", "commit": "a" * 40}


def test_the_credential_a_repo_is_read_with_is_offered_in_settings_and_can_be_another(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from harness import credentials
    from orchestrator.settings import CredentialStore

    monkeypatch.setattr(credentials, "_stored", {})  # what this test stores is not other tests'

    store = _sources(tmp_path, Source("team", "https://git.example.com/team/jobs.git", "main"))
    store.set("mine", {"url": "https://git.example.com/me/jobs.git", "token": "MY_JOBS_TOKEN"})
    settings = CredentialStore(tmp_path / "credentials.enc", Fernet.generate_key())
    test = TestClient(create_app(FakeBackend(), JobRunner([], sources=store), settings=settings),
                      base_url="http://127.0.0.1:8700")
    rows = {c["name"]: c for c in test.get("/api/settings/credentials").json()}
    assert "JOBS_GIT_TOKEN" in rows and "Reads the git repos of jobs" in rows["JOBS_GIT_TOKEN"]["use"]
    assert "MY_JOBS_TOKEN" in rows
    assert test.put("/api/settings/credentials/MY_JOBS_TOKEN", headers=CHANGE, json={"value": "t"}).status_code == 200
    assert {r["name"]: r["token_set"] for r in store.listing()["sources"]} == {"team": False, "mine": True}
