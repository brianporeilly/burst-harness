"""`harness oauth CLIENT_FILE`: sign in once, for the refresh token a job's secret is exchanged from (job.py,
token_url).

CLIENT_FILE is an OAuth client of the "desktop app" kind, as its console downloads it: JSON with an
"installed" table (client_id, client_secret, auth_uri, token_uri). The sign-in is the loopback flow with PKCE:
a page on 127.0.0.1 takes the code the browser is sent back with, which is exchanged for tokens. The refresh
token is written to a file only you can read, and never printed: set it where the job's driver reads its
credentials (the orchestrator's Settings), then delete the file.

By default it asks to read Gmail and Google Calendar, which is all a report-only job can use anyway.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

DEFAULT_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",
                  "https://www.googleapis.com/auth/calendar.readonly")
WAIT = 300  # seconds for the browser to come back


class OAuthError(RuntimeError):
    """A sign-in that did not end in a refresh token."""


def client(path: Path) -> dict[str, str]:
    try:
        data = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError) as err:
        raise OAuthError(f"{path}: not a client file ({err}).") from err
    found = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(found, dict) or not all(isinstance(found.get(k), str) for k in ("client_id", "auth_uri",
                                                                                         "token_uri")):
        raise OAuthError(f"{path}: not a desktop app's client (an \"installed\" table with client_id, auth_uri "
                         "and token_uri). Make one of the \"Desktop app\" kind and download its JSON.")
    return found


def sign_in(client_file: Path, out: Path, scopes: tuple[str, ...] = DEFAULT_SCOPES, *,
            browse: Callable[[str], Any] | None = None, wait: float = WAIT) -> Path:
    """Sign in in the browser, and write the refresh token to `out` (mode 0600). Returns `out`."""
    found = client(client_file)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    came_back: dict[str, str] = {}

    class Back(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            if query.get("state") != state:
                self._say(400, "This is not the sign-in this command started. Close this tab.")
                return
            came_back.update(query)
            self._say(200, "Signed in. You can close this tab and go back to the terminal."
                      if "code" in query else f"The sign-in did not finish: {query.get('error', 'no code')}.")

        def _say(self, status: int, text: str) -> None:
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Back)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = found["auth_uri"] + "?" + urlencode({
        "client_id": found["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(scopes), "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        # A refresh token, every time, rather than only on the first consent.
        "access_type": "offline", "prompt": "consent",
    })
    print(f"Sign in here (opening it in your browser):\n\n  {url}\n", flush=True)
    threading.Thread(target=browse or webbrowser.open, args=(url,), daemon=True).start()
    deadline = time.monotonic() + wait
    try:
        while not came_back and time.monotonic() < deadline:
            server.timeout = max(0.1, min(1.0, deadline - time.monotonic()))
            server.handle_request()
    finally:
        server.server_close()
    if "code" not in came_back:
        raise OAuthError(f"The sign-in did not finish: {came_back.get('error') or 'no answer in time'}.")

    form = {"grant_type": "authorization_code", "code": came_back["code"], "redirect_uri": redirect,
            "client_id": found["client_id"], "code_verifier": verifier}
    if found.get("client_secret"):
        form["client_secret"] = found["client_secret"]
    request = urllib.request.Request(found["token_uri"], data=urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            tokens = json.loads(response.read())
    except urllib.error.HTTPError as err:
        raise OAuthError(f"{found['token_uri']} refused the code: HTTP {err.code}.") from None
    except (OSError, ValueError) as err:
        raise OAuthError(f"Could not exchange the code at {found['token_uri']}: {err}") from None
    refresh = tokens.get("refresh_token") if isinstance(tokens, dict) else None
    if not isinstance(refresh, str) or not refresh:
        raise OAuthError("Signed in, but no refresh token came back.")
    out = Path(out).expanduser()
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(refresh + "\n")
    return out
