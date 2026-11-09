"""OAuth refresh tokens for jobs: signing in once (harness/oauth.py), and a job's secret exchanged for an access
token as its run starts (job.py, token_url). Against a fake authorization server: no real sign-in."""

import base64
import hashlib
import json
import stat
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from harness import cli, job, oauth
from harness.exec_target import LocalTarget


class FakeAuth(BaseHTTPRequestHandler):
    """A token endpoint: a code for a refresh token, and a refresh token for an access token."""

    received: list = []

    def do_POST(self):
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
        type(self).received.append(form)
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") == "1//refresh-not-real":
            self._json(200, {"access_token": f"ya29.access-{len(type(self).received)}", "expires_in": 3599})
        elif form.get("grant_type") == "authorization_code" and form.get("code") == "the-code":
            self._json(200, {"access_token": "ya29.first", "refresh_token": "1//refresh-not-real"})
        else:
            self._json(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def auth():
    FakeAuth.received = []
    server = HTTPServer(("127.0.0.1", 0), FakeAuth)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/token"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _fresh_tokens(monkeypatch):
    monkeypatch.setattr(job, "_ACCESS_TOKENS", {})


def _client_file(tmp_path, token_url):
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"installed": {"client_id": "id.apps.example.com", "client_secret": "client-not-real",
                                              "auth_uri": "https://accounts.example.com/auth",
                                              "token_uri": token_url}}))
    return path


def _browser(answer=None):
    """Stands in for the browser: signs in at once and is sent back to the page with a code."""
    seen = {}

    def browse(url):
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        seen.update(query)
        back = {"code": "the-code", "state": query["state"], **(answer or {})}
        urllib.request.urlopen(query["redirect_uri"] + "?" + "&".join(f"{k}={v}" for k, v in back.items()),
                               timeout=10).read()

    return browse, seen


def test_signing_in_writes_the_refresh_token_for_you_alone_and_never_prints_it(tmp_path, auth, capsys):
    browse, seen = _browser()
    out = oauth.sign_in(_client_file(tmp_path, auth), tmp_path / "token.txt", browse=browse, wait=10)
    assert out.read_text() == "1//refresh-not-real\n" and stat.S_IMODE(out.stat().st_mode) == 0o600
    assert "refresh-not-real" not in capsys.readouterr().out
    assert seen["scope"] == " ".join(oauth.DEFAULT_SCOPES) and seen["access_type"] == "offline"
    assert seen["redirect_uri"].startswith("http://127.0.0.1:")
    # PKCE: the verifier sent with the code is the one the challenge was made from.
    (sent,) = FakeAuth.received
    challenge = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest()).rstrip(b"=")
    assert seen["code_challenge"] == challenge.decode() and seen["code_challenge_method"] == "S256"
    assert sent["client_secret"] == "client-not-real" and sent["redirect_uri"] == seen["redirect_uri"]


def test_a_sign_in_turned_down_or_not_a_desktop_client_says_so(tmp_path, auth):
    browse, _ = _browser({"code": "", "error": "access_denied"})
    with pytest.raises(oauth.OAuthError, match="access_denied"):
        oauth.sign_in(_client_file(tmp_path, auth), tmp_path / "token.txt", browse=lambda url: browse(url), wait=10)
    assert not (tmp_path / "token.txt").exists()
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "x"}}))
    with pytest.raises(oauth.OAuthError, match="Desktop app"):
        oauth.client(web)


def test_the_command_runs_the_sign_in(tmp_path, auth, monkeypatch, capsys):
    browse, _ = _browser()
    monkeypatch.setattr(oauth.webbrowser, "open", browse)
    assert cli.main(["oauth", str(_client_file(tmp_path, auth)), "--out", str(tmp_path / "t.txt")]) == 0
    assert (tmp_path / "t.txt").read_text().strip() == "1//refresh-not-real"
    assert "delete" in capsys.readouterr().out


MAIL = """
[job]
mode = "report"
[job.secrets.mail]
env = "GOOGLE_REFRESH_TOKEN"
host = "gmail.googleapis.com"
token_url = "TOKEN_URL"
client_id_env = "GOOGLE_CLIENT_ID"
client_secret_env = "GOOGLE_CLIENT_SECRET"
[job.secrets.calendar]
env = "GOOGLE_REFRESH_TOKEN"
host = "www.googleapis.com"
token_url = "TOKEN_URL"
client_id_env = "GOOGLE_CLIENT_ID"
client_secret_env = "GOOGLE_CLIENT_SECRET"
"""


def _mail_job(tmp_path, auth, toml=MAIL):
    path = tmp_path / "brief"
    path.mkdir(parents=True)
    (path / "job.toml").write_text(toml.replace("TOKEN_URL", auth))
    (path / "prompt.md").write_text("Brief me.\n")
    return path


def _google(monkeypatch):
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "1//refresh-not-real")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id.apps.example.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-not-real")


def test_the_gateway_adds_an_access_token_exchanged_once_for_the_refresh_token(tmp_path, auth, monkeypatch):
    _google(monkeypatch)
    spec = job.load(_mail_job(tmp_path, auth))
    assert spec.secret_envs == ["GOOGLE_REFRESH_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"]
    job.check(spec)
    seen = {}
    monkeypatch.setattr(job.containers, "acquire", lambda cfg, **kw: seen.update(kw) or LocalTarget(repo=str(tmp_path)))
    job._acquire(job.config(spec), spec)
    assert seen["inject"] == ({"host": "gmail.googleapis.com", "headers": {"Authorization": "Bearer ya29.access-1"}},
                              {"host": "www.googleapis.com", "headers": {"Authorization": "Bearer ya29.access-1"}})
    assert len(FakeAuth.received) == 1  # one exchange for both hosts
    assert FakeAuth.received[0] == {"grant_type": "refresh_token", "refresh_token": "1//refresh-not-real",
                                    "client_id": "id.apps.example.com", "client_secret": "client-not-real"}
    assert "exchanged for an access token at" in job.describe(spec, job.config(spec))


def test_a_refused_refresh_token_says_to_sign_in_again_without_the_token(tmp_path, auth, monkeypatch):
    _google(monkeypatch)
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "1//revoked-not-real")
    spec = job.load(_mail_job(tmp_path, auth))
    with pytest.raises(job.JobError, match="invalid_grant.*sign in again") as err:
        spec.secrets[0].rule()
    assert "revoked-not-real" not in str(err.value) and "client-not-real" not in str(err.value)


def test_a_job_with_a_refresh_token_needs_its_client_and_says_what_is_missing(tmp_path, auth, monkeypatch):
    with pytest.raises(job.JobError, match="client_id_env"):
        job.load(_mail_job(tmp_path, auth, MAIL.replace('client_id_env = "GOOGLE_CLIENT_ID"\n', "", 1)))
    with pytest.raises(job.JobError, match="https token_url"):
        job.load(_mail_job(tmp_path / "b", "http://tokens.example.com/token"))
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "1//refresh-not-real")
    with pytest.raises(job.JobError, match="GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET"):
        job.check(job.load(_mail_job(tmp_path / "c", auth)))
