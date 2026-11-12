"""Forges (forge.py): which host gets which token, and a Forgejo's PRs opened and read through its API."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness import containers, credentials, forge, git, terminal
from harness.config import HarnessConfig

FORGEJO = "forgejo=https://forgejo.example.com"
REPO = "https://forgejo.example.com/me/proj.git"


@pytest.fixture(autouse=True)
def no_tokens(monkeypatch):
    for name in (*credentials.GIT, "FORGEJO_TOKEN", "LOCAL_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_forges_are_github_and_those_listed_checked_when_the_config_loads():
    cfg = HarnessConfig(forges=(FORGEJO, " gitea = https://git.example.com:3000/ "))
    assert [(f.name, f.kind, f.host, f.url) for f in forge.forges(cfg)] == [
        ("github", "github", "github.com", "https://github.com"),
        ("forgejo", "forgejo", "forgejo.example.com", "https://forgejo.example.com"),
        ("gitea", "forgejo", "git.example.com:3000", "https://git.example.com:3000")]
    assert forge.forges(cfg)[1].token_vars == ("FORGEJO_TOKEN",)
    for entry, said in (("https://forgejo.example.com", "NAME=URL"), ("Forgejo=https://f.example.com", "NAME=URL"),
                        ("f=http://f.example.com", "not https://host"), ("f=https://f.example.com/sub", "not https://host"),
                        ("f=https://me:pw@f.example.com", "not https://host"), ("g=https://github.com", "listed already")):
        with pytest.raises(forge.ForgeError, match=said):
            HarnessConfig.load(None, {"forges": (entry,)}, user_config=None)
    with pytest.raises(forge.ForgeError, match="listed already"):
        HarnessConfig.load(None, {"forges": (FORGEJO, "forgejo=https://other.example.com")}, user_config=None)


def test_the_setting_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("HARNESS_FORGES", FORGEJO)
    assert [f.name for f in forge.forges(HarnessConfig.load(None, {}, user_config=None))] == ["github", "forgejo"]


def test_a_token_goes_to_its_own_forges_host_alone(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh-sekret")
    monkeypatch.setenv("FORGEJO_TOKEN", "fj-sekret")
    cfg = HarnessConfig(forges=(FORGEJO,))

    def sent(url, config=cfg):
        auth = git.auth_args(url, config)
        return base64.b64decode(auth[1].rsplit(" ", 1)[1]).decode().partition(":")[2] if auth else None

    assert sent("https://github.com/me/proj.git") == "gh-sekret"
    assert sent(REPO) == "fj-sekret"
    assert sent("https://FORGEJO.example.com/me/proj") == "fj-sekret"  # host names are not case-sensitive
    assert sent("https://git.elsewhere.example/me/proj.git") is None  # no forge: no token, not GitHub's
    assert sent(REPO, None) is None  # without a config, GitHub alone
    assert sent("https://github.com/me/proj.git", None) == "gh-sekret"
    assert forge.for_url(cfg, "git@forgejo.example.com:me/proj.git").name == "forgejo"
    assert forge.for_url(cfg, "/srv/git/proj.git") is None


def test_the_gateway_adds_the_forges_token_for_its_repo(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh-sekret")
    cfg = HarnessConfig(forges=(FORGEJO,), repo_url=REPO, egress_inject=("git",))
    with pytest.raises(containers.ContainerError, match="one of FORGEJO_TOKEN"):
        containers.inject_rules(cfg)
    monkeypatch.setenv("FORGEJO_TOKEN", "fj-sekret")
    rules = containers.inject_rules(cfg)
    assert {r["host"] for r in rules} == {"forgejo.example.com"} and {r["path"] for r in rules} == {
        "/me/proj.git/*", "/me/proj/*"}
    assert rules[0]["headers"]["Authorization"] == "Basic " + base64.b64encode(b"x-access-token:fj-sekret").decode()
    with pytest.raises(containers.ContainerError, match="not a forge the harness knows"):
        containers.inject_rules(HarnessConfig(repo_url=REPO, egress_inject=("git",)))


def test_a_task_is_granted_the_forges_tokens():
    granted = credentials.for_task(HarnessConfig(forges=(FORGEJO,)))
    assert "FORGEJO_TOKEN" in granted and "GH_TOKEN" in granted
    assert "FORGEJO_TOKEN" not in credentials.for_task(HarnessConfig())


class FakeForgejo:
    """A Forgejo's API on loopback: it records each request, and answers from `routes`."""

    def __init__(self):
        self.seen, self.routes = [], {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
                status, said = fake.routes.get((self.command, self.path), (404, {"message": "not found"}))
                data = json.dumps(said).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        port = self.server.server_port
        self.forge = forge.Forge("local", "forgejo", f"127.0.0.1:{port}", f"http://127.0.0.1:{port}")
        self.page = f"https://127.0.0.1:{port}"


@pytest.fixture
def fake(monkeypatch):
    found = FakeForgejo()
    monkeypatch.setenv("LOCAL_TOKEN", "fj-sekret")
    yield found
    found.server.shutdown()


def test_a_pr_is_opened_through_the_api_with_the_forges_token(fake):
    fake.routes[("POST", "/api/v1/repos/me/proj/pulls")] = (201, {"html_url": f"{fake.page}/me/proj/pulls/7"})
    url = forge.forgejo_open_pr(fake.forge, f"{fake.page}/me/proj.git", head="harness/t", base="main",
                                title="Fix it", body="Done.", draft=True)
    assert url == f"{fake.page}/me/proj/pulls/7"
    method, path, auth, body = fake.seen[0]
    assert (method, path, auth) == ("POST", "/api/v1/repos/me/proj/pulls", "token fj-sekret")
    assert body == {"head": "harness/t", "base": "main", "title": "WIP: Fix it", "body": "Done."}  # draft: WIP


def test_a_pr_already_open_from_the_branch_is_the_tasks_pr(fake):
    fake.routes[("POST", "/api/v1/repos/me/proj/pulls")] = (409, {"message": "pull request already exists"})
    fake.routes[("GET", "/api/v1/repos/me/proj/pulls?state=open&limit=50")] = (200, [
        {"head": {"ref": "harness/other"}, "html_url": f"{fake.page}/me/proj/pulls/3"},
        {"head": {"ref": "harness/t"}, "html_url": f"{fake.page}/me/proj/pulls/4"}])
    assert forge.forgejo_open_pr(fake.forge, f"{fake.page}/me/proj", head="harness/t", base="main", title="t",
                                 body="b", draft=False) == f"{fake.page}/me/proj/pulls/4"
    fake.routes[("POST", "/api/v1/repos/me/proj/pulls")] = (403, {"message": "token does not have scope"})
    with pytest.raises(forge.ForgeError, match=r"refused the PR \(403\): token does not have scope"):
        forge.forgejo_open_pr(fake.forge, f"{fake.page}/me/proj", head="harness/t", base="main", title="t",
                              body="b", draft=False)


def test_a_prs_state_is_read_through_the_api(fake):
    for number, said, state in ((1, {"state": "open", "merged": False}, "open"),
                                (2, {"state": "closed", "merged": True}, "merged"),
                                (3, {"state": "closed", "merged": False}, "closed")):
        fake.routes[("GET", f"/api/v1/repos/me/proj/pulls/{number}")] = (200, said)
        assert forge.forgejo_pr_state(fake.forge, f"{fake.page}/me/proj/pulls/{number}") == state
    assert fake.seen[0][2] == "token fj-sekret"
    with pytest.raises(forge.ForgeError, match=r"\(404\)"):
        forge.forgejo_pr_state(fake.forge, f"{fake.page}/me/proj/pulls/9")
    with pytest.raises(forge.ForgeError, match="not a local PR's URL"):
        forge.forgejo_pr_state(fake.forge, f"{fake.page}/me/proj/pull/9")


def test_git_sends_a_forges_prs_to_its_api_and_github_s_to_gh(monkeypatch):
    opened, read = [], []
    monkeypatch.setattr(forge, "forgejo_open_pr", lambda host, url, **kw: opened.append((host.name, url, kw))
                        or "https://forgejo.example.com/me/proj/pulls/1")
    monkeypatch.setattr(forge, "forgejo_pr_state", lambda host, url: read.append(url) or "merged")
    cfg = HarnessConfig(forges=(FORGEJO,))
    assert git.open_pr(repo="forgejo.example.com/me/proj", head="h", title="t", body="b", base="main", draft=False,
                       cfg=cfg) == "https://forgejo.example.com/me/proj/pulls/1"
    assert opened == [("forgejo", "https://forgejo.example.com/me/proj",
                       {"head": "h", "base": "main", "title": "t", "body": "b", "draft": False})]
    assert git.pr_state("https://forgejo.example.com/me/proj/pulls/1", cfg) == "merged"
    with pytest.raises(git.GitError, match="not a forge the harness knows"):
        git.open_pr(repo="git.elsewhere.example/me/proj", head="h", title="t", body="b", base="main", draft=False,
                    cfg=cfg)
    with pytest.raises(git.GitError, match="not a forge the harness knows"):
        git.pr_state("https://forgejo.example.com/me/proj/pulls/1")  # without the config, GitHub alone


def test_a_task_finished_on_a_forge_opens_its_pr_there(monkeypatch):
    calls = []
    monkeypatch.setattr(terminal.git, "open_pr", lambda **kw: calls.append(kw) or "https://forgejo.example.com/p/1")
    monkeypatch.setattr(terminal, "_wrap_up", lambda ctx, title: ["Pushed."])
    monkeypatch.setattr(terminal, "update_doc", lambda ctx, **kw: None)

    class Ctx:
        cfg = HarnessConfig(forges=(FORGEJO,))
        repo_slug, branch, terminal, pr_url = "forgejo.example.com/me/proj", "harness/t", None, None

    ctx = Ctx()
    assert "Opened PR: https://forgejo.example.com/p/1" in terminal.finish(ctx, title="t", body="b")
    assert calls[0]["cfg"] is ctx.cfg and calls[0]["repo"] == "forgejo.example.com/me/proj"
