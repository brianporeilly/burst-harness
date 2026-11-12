"""Where a repo is hosted, which decides the token sent to it and how its pull requests are opened and read.

GitHub (github.com) is built in: its token is HARNESS_GIT_TOKEN, GH_TOKEN or GITHUB_TOKEN, and its PRs go through
`gh` (git.py). Others are listed in `forges` as NAME=URL, each a Forgejo (or Gitea: the same API):

    forges = ["forgejo=https://forgejo.example.com"]

Its token is the credential <NAME>_TOKEN (FORGEJO_TOKEN), and its PRs go through its API from the driver: POST
/api/v1/repos/OWNER/REPO/pulls to open one, GET .../pulls/N for its state. Forgejo has no draft PRs; a draft's
title starts "WIP: ", which Forgejo shows as one.

A token goes only to its own forge's host: a repo on a host that is neither github.com nor listed gets none,
and can be cloned only if it is public. git sends either token as HTTP Basic credentials (the user name is not
checked), so the gateway's egress_inject git works for both.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from . import credentials

if TYPE_CHECKING:  # config checks the entries with forges(), so this module cannot import it
    from .config import HarnessConfig

NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
TIMEOUT = 30
# https://HOST/OWNER/REPO/pulls/N, a Forgejo PR's page.
FORGEJO_PR = re.compile(r"https://(?P<host>[A-Za-z0-9.:-]+)/(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)"
                        r"/pulls/(?P<number>[0-9]+)")


class ForgeError(ValueError):
    """A `forges` entry that cannot be used, or a forge's API refusing or failing."""


@dataclass(frozen=True)
class Forge:
    name: str
    kind: str  # "github" or "forgejo"
    host: str  # host[:port], as it appears in the repo's URL
    url: str = ""  # https://host[:port], for an API

    @property
    def token_vars(self) -> tuple[str, ...]:
        """The credentials holding its token, the first set of them used."""
        return credentials.GIT if self.kind == "github" else (token_var(self.name),)

    def token(self) -> str:
        return credentials.first(self.token_vars)


GITHUB = Forge("github", "github", "github.com", "https://github.com")


def token_var(name: str) -> str:
    """FORGEJO_TOKEN for a forge named forgejo."""
    return re.sub(r"[^A-Z0-9_]", "_", name.upper()) + "_TOKEN"


def forges(cfg: HarnessConfig) -> list[Forge]:
    """GitHub, then `forges` as Forge, checked: a plain lowercase name and an https:// URL with nothing after
    the host, each name and host once. Raises ForgeError."""
    found = [GITHUB]
    for entry in cfg.forges:
        name, sep, url = (part.strip() for part in entry.partition("="))
        if not sep or not NAME.fullmatch(name):
            raise ForgeError(f"A forges entry is NAME=URL, with a lowercase name (got {entry!r}).")
        parts = urlsplit(url.rstrip("/"))
        if parts.scheme != "https" or not parts.hostname or parts.path or parts.query or parts.username:
            raise ForgeError(f"forges {name}: {url!r} is not https://host, the forge's address.")
        host = parts.netloc.lower()
        if any(f.name == name or f.host == host for f in found):
            raise ForgeError(f"forges {name}: its name or its host ({host}) is listed already.")
        found.append(Forge(name, "forgejo", host, f"https://{host}"))
    return found


def _host(url: str) -> str:
    """host[:port] of an https URL, or of an ssh one (git@host:owner/repo)."""
    url = url.strip()
    if "://" in url:
        parts = urlsplit(url)
        return (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    match = re.match(r"^[^@/]+@(?P<host>[^/:]+):", url)
    return match["host"] if match else ""


def for_url(cfg: HarnessConfig | None, url: str) -> Forge | None:
    """The forge hosting a repo's (or a PR's) URL; None for a host that is none of them. Without a config,
    GitHub alone."""
    host = _host(url).lower()
    return next((f for f in (forges(cfg) if cfg is not None else [GITHUB]) if f.host == host), None) if host else None


def token_for(cfg: HarnessConfig | None, url: str) -> str:
    """The token to send a repo's host: its forge's, or none."""
    forge = for_url(cfg, url)
    return forge.token() if forge else ""


def basic_credential(secret: str) -> str:
    """A token as HTTP Basic credentials, which GitHub and Forgejo both take for git."""
    return base64.b64encode(f"x-access-token:{secret}".encode()).decode() if secret else ""


def all_tokens(cfg: HarnessConfig | None) -> list[str]:
    """Every forge's token that has a value, for keeping them out of what is shown."""
    found = forges(cfg) if cfg is not None else [GITHUB]
    return [secret for forge in found if (secret := forge.token())]


# --- Forgejo's API --------------------------------------------------------------


def _api(forge: Forge, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    request = urllib.request.Request(
        f"{forge.url}/api/v1{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "burst-harness",
                 **({"Authorization": f"token {secret}"} if (secret := forge.token()) else {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as err:
        try:
            said = json.loads(err.read() or b"null")
        except ValueError:
            said = None
        return err.code, said
    except (OSError, ValueError) as err:
        raise ForgeError(f"{forge.name} ({forge.url}) could not be reached: {err}") from err


def _message(said: object) -> str:
    return str(said.get("message") or "") if isinstance(said, dict) else ""


def _repo_path(url: str) -> tuple[str, str]:
    match = re.match(r"^(?:https://[^/]+/|[^@/]+@[^/:]+:)(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$", url.strip())
    if not match:
        raise ForgeError(f"{url!r} is not a repo's URL (https://host/owner/repo).")
    return match["owner"], match["repo"]


def forgejo_open_pr(forge: Forge, repo_url: str, *, head: str, base: str, title: str, body: str,
                    draft: bool) -> str:
    """Open a PR on a Forgejo and return its page's URL; the PR already open from `head`, if there is one."""
    owner, repo = _repo_path(repo_url)
    status, said = _api(forge, "POST", f"/repos/{owner}/{repo}/pulls",
                        {"head": head, "base": base, "title": f"WIP: {title}" if draft else title, "body": body})
    if status in (200, 201) and isinstance(said, dict) and said.get("html_url"):
        return str(said["html_url"])
    if status == 409:  # one is open from this branch already: a later burst's push updated it
        found, listed = _api(forge, "GET", f"/repos/{owner}/{repo}/pulls?state=open&limit=50")
        for pr in listed if found == 200 and isinstance(listed, list) else []:
            if isinstance(pr, dict) and (pr.get("head") or {}).get("ref") == head and pr.get("html_url"):
                return str(pr["html_url"])
    raise ForgeError(f"{forge.name} refused the PR ({status}): {_message(said) or 'no reason given'}")


def forgejo_pr_state(forge: Forge, pr_url: str) -> str:
    """A Forgejo PR's state: "open", "merged" or "closed"."""
    match = FORGEJO_PR.fullmatch(pr_url)
    if not match:
        raise ForgeError(f"{pr_url!r} is not a {forge.name} PR's URL.")
    status, said = _api(forge, "GET", f"/repos/{match['owner']}/{match['repo']}/pulls/{match['number']}")
    if status != 200 or not isinstance(said, dict):
        raise ForgeError(f"{forge.name} could not say how {pr_url} is ({status}): {_message(said) or 'no reason'}")
    if said.get("merged"):
        return "merged"
    return "open" if said.get("state") == "open" else "closed"
