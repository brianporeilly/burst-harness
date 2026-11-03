"""Provider plugins for tests, as `tests.fake_provider:NAME`: they declare things, and build nothing."""

import time

from harness.models import TargetEndpoint

PROVIDER = "tests.fake_provider:build"


def build(cfg):
    raise RuntimeError("the fake provider builds no model")


build.credentials = ("EXAMPLE_CLIENT_ID", "EXAMPLE_CLIENT_SECRET")


def plain(cfg):
    raise RuntimeError("the fake provider builds no model")


class Tokens:
    """A target endpoint's token source: each call gives the next (token, seconds it lasts) of `given`,
    then the last again."""

    def __init__(self, *given):
        self.given, self.calls = list(given), 0

    def __call__(self, *, force_refresh=False):
        from anthropic.lib.credentials import AccessToken

        token, life = self.given[min(self.calls, len(self.given) - 1)]
        self.calls += 1
        return AccessToken(token=token, expires_at=int(time.time() + life) if life is not None else None)


TOKENS = Tokens(("token-1", None))


def gateway(cfg):
    raise RuntimeError("the fake provider builds no model")


gateway.target_endpoint = lambda cfg: TargetEndpoint(base_url="https://ai.example.com/anthropic",
                                                     token=TOKENS, env={"EXAMPLE_FLAG": "1"})
gateway.credentials = ("EXAMPLE_CLIENT_SECRET",)
