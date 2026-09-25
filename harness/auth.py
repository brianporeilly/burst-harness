"""Bearer-token credentials for Anthropic-compatible endpoints.

Most runs need none of this: the anthropic provider reads ANTHROPIC_API_KEY. This
is for endpoints behind a proxy that wants a short-lived bearer token instead --
the equivalent of Claude Code's `apiKeyHelper`.

    command  HARNESS_TOKEN_COMMAND. Run it; stdout is the token. Re-run to refresh.
    file     HARNESS_TOKEN_FILE. Read it; re-read to refresh. For a token another
             process keeps current, such as a mounted Secret.

Both are Anthropic `credentials=` providers, so the SDK's TokenCache re-invokes
them to refresh; neither pins a token for the process lifetime. Anything more
involved (an OAuth grant, a vendor CLI) belongs in a provider plugin -- see
models.py -- not here.
"""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from anthropic.lib.credentials import AccessToken

from .config import HarnessConfig

# Report expiry early: the SDK refreshes only 120s out, a window tuned for
# ~10-minute tokens, and a slow command can eat most of that.
EXPIRY_SAFETY_MARGIN_SECONDS = 300


class AuthError(RuntimeError):
    """The token source is missing or returned nothing."""


def _token_expiry(token: str) -> int | None:
    """Read `exp` from a JWT payload so the token cache can refresh on schedule.

    Decoded without verifying the signature -- the endpoint does that; we only
    need the timestamp. Returns None for a non-JWT, which the cache treats as
    never-expiring and refreshes only after a 401.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
    except Exception:
        return None
    return int(exp) - EXPIRY_SAFETY_MARGIN_SECONDS


def _wrap(token: str) -> AccessToken:
    if not token:
        raise AuthError("Token source returned an empty token.")
    return AccessToken(token=token, expires_at=_token_expiry(token))


def command_token(command: str) -> Callable[..., AccessToken]:
    argv = shlex.split(command)

    def provider(*, force_refresh: bool = False) -> AccessToken:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, check=True, timeout=120)
        except FileNotFoundError as err:
            raise AuthError(f"{argv[0]} not found on PATH.") from err
        except subprocess.CalledProcessError as err:
            raise AuthError(f"{command} failed: {err.stderr.strip()}") from err
        return _wrap(proc.stdout.strip())

    return provider


def file_token(path: str) -> Callable[..., AccessToken]:
    resolved = Path(path).expanduser()

    def provider(*, force_refresh: bool = False) -> AccessToken:
        if not resolved.is_file():
            raise AuthError(f"No token file at {resolved}.")
        return _wrap(resolved.read_text().strip())

    return provider


def credentials(cfg: HarnessConfig) -> Callable[..., AccessToken] | None:
    """The configured token provider, or None to use the SDK's own API-key chain."""
    if cfg.token_command and cfg.token_file:
        raise AuthError("Set token_command or token_file, not both.")
    if cfg.token_command:
        return command_token(cfg.token_command)
    if cfg.token_file:
        return file_token(cfg.token_file)
    return None
