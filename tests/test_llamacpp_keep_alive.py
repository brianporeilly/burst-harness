"""A llama.cpp server that keeps connections open, as llama-server does, across the harness's calls of the agent:
each runs on an event loop of its own (Strands' run_async), so a connection one call left open belonged to a
loop that was gone by the next, which then died closing it ("Event loop is closed")."""

import json

from harness import cli

from . import fake_llama
from .conftest import git


def _result(out):
    return json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])


def test_a_cut_off_reply_is_retried_on_a_kept_alive_connection(tmp_path, origin, capsys):
    server, requests = fake_llama.serve([{"cut": "I was going to say"},
                                         {"tool": "pause_for_review", "args": {"summary": "s", "next_step": "n"}},
                                         {"text": "Paused."}], keep_alive=True)
    try:
        work = tmp_path / "work"
        git(tmp_path, "clone", "-q", str(origin), str(work))
        url = f"http://127.0.0.1:{server.server_port}"
        code = cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "t1",
                         "--task", "Go."])
        result = _result(capsys.readouterr().out)
    finally:
        server.shutdown()
    assert code == cli.EXIT_DONE and result["state"] == "paused" and result["metrics"]["cut_off"] == 1
    assert len(requests) >= 2  # the cut-off reply, then the retry on a new loop that went through


def test_a_client_for_each_loop_and_the_same_on_one():
    import asyncio

    from harness import models
    from harness.config import HarnessConfig

    model = models._llamacpp(HarnessConfig(provider="llamacpp", base_url="http://127.0.0.1:9", model_id="m"))
    first = model.client  # outside any loop: the one Strands made

    async def twice():
        return model.client, model.client

    a, b = asyncio.run(twice())
    assert a is b is first  # its first loop adopts it
    c, d = asyncio.run(twice())
    assert c is d and c is not first  # another loop: a fresh pool
    assert str(c.base_url).rstrip("/") == "http://127.0.0.1:9" and c.timeout == first.timeout
