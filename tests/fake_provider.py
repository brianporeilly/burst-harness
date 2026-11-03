"""A provider plugin for tests, as `tests.fake_provider:build`: it declares credentials, and builds nothing."""

PROVIDER = "tests.fake_provider:build"


def build(cfg):
    raise RuntimeError("the fake provider builds no model")


build.credentials = ("EXAMPLE_CLIENT_ID", "EXAMPLE_CLIENT_SECRET")


def plain(cfg):
    raise RuntimeError("the fake provider builds no model")
