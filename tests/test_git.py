import base64

import pytest

from harness import git
from harness.exec_target import LocalTarget

from .conftest import git as sh_git


@pytest.mark.parametrize("url,slug", [
    ("https://github.com/o/r.git", "github.com/o/r"),
    ("https://github.com/o/r", "github.com/o/r"),
    ("https://x-access-token:abc@github.com/o/r.git", "github.com/o/r"),
    ("git@github.com:o/r.git", "github.com/o/r"),
    ("ssh://git@git.example.com:2222/o/r.git", "git.example.com/o/r"),
    ("/srv/git/r.git", None),
    ("file:///srv/git/r.git", None),
])
def test_repo_slug(url, slug):
    assert git.repo_slug(url) == slug


def test_redact_url():
    assert git.redact_url("https://user:tok@github.com/o/r") == "https://github.com/o/r"
    assert git.redact_url("git@github.com:o/r.git") == "git@github.com:o/r.git"


def test_auth_args_only_for_https_with_token(monkeypatch):
    assert git.auth_args("https://github.com/o/r") == ()
    monkeypatch.setenv("GH_TOKEN", "sekret")
    assert git.auth_args("git@github.com:o/r.git") == ()
    flag, value = git.auth_args("https://github.com/o/r")
    assert flag == "-c"
    assert base64.b64decode(value.rsplit(" ", 1)[1]).decode() == "x-access-token:sekret"


def test_harness_token_wins(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.setenv("HARNESS_GIT_TOKEN", "harness")
    assert git.token() == "harness"


def test_token_never_in_errors(monkeypatch, origin, tmp_path):
    from harness.config import HarnessConfig

    monkeypatch.setenv("LOCAL_TOKEN", "sekret-token")  # a forge's token, sent to its host alone
    work = tmp_path / "work"
    sh_git(tmp_path, "clone", "-q", str(origin), str(work))
    target = LocalTarget(repo=str(work))
    auth = git.auth_args("https://127.0.0.1:9/o/r", HarnessConfig(forges=("local=https://127.0.0.1:9",)))
    assert auth  # sent, so there is something to keep out of the error
    with pytest.raises(git.GitError) as err:
        git.run(target, "push", "https://127.0.0.1:9/o/r", "main", auth=auth)
    message = str(err.value)
    assert "sekret-token" not in message
    assert base64.b64encode(b"x-access-token:sekret-token").decode() not in message
    # ...and the header is never written into the repo's config.
    assert "extraheader" not in (work / ".git" / "config").read_text().lower()


def test_clone_start_and_sync(origin, tmp_path):
    target = LocalTarget(repo=str(tmp_path / "nested" / "repo"))
    assert not git.is_repo(target)
    git.clone(target, str(origin))
    assert git.is_repo(target)
    assert "Created harness/t" in git.start_branch(target, "harness/t", base="main", remote="origin")
    (tmp_path / "nested" / "repo" / "f").write_text("x")
    git.commit_all(target, "work")
    git.push(target, "harness/t", remote="origin")

    fresh = LocalTarget(repo=str(tmp_path / "fresh"))
    git.clone(fresh, str(origin))
    assert git.remote_has_branch(fresh, "harness/t", remote="origin")
    assert "up to date" in git.sync_branch(fresh, "harness/t", remote="origin")
    assert git.current_branch(fresh) == "harness/t"
    assert (tmp_path / "fresh" / "f").read_text() == "x"
