"""Other agents: profiles, a router picking the lead, and a lead delegating to them, on fake llama servers."""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from harness import agents, cli, job
from harness.config import HarnessConfig

from . import fake_llama
from .test_job import _local, _write_job

DEAD = "http://127.0.0.1:9"  # nothing listens: a model that must not be called


@pytest.fixture
def llama():
    servers = []

    def start(script, side="Side reply"):
        server, requests = fake_llama.serve(script, side=side)
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}", requests

    yield start
    for server in servers:
        server.shutdown()


def _profiles(**urls):
    return {name: {"provider": "llamacpp", "base_url": url, "model_id": name, "use_for": f"{name} work"}
            for name, url in urls.items()}


def _job(tmp_path, settings):
    toml = f'provider = "llamacpp"\nbase_url = "{DEAD}"\nmodel_id = "top"\nmodel_retries = 0\n'
    toml += "\n".join(f"{key} = {json.dumps(value)}" for key, value in settings.items() if key != "agents")
    for name, profile in settings.get("agents", {}).items():
        toml += f"\n[agents.{name}]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in profile.items())
    spec = job.load(_write_job(tmp_path, toml + '\n[job]\noutput = ["stdout"]\n'))
    return spec, job.config(spec)


def _outcome(out):
    return json.loads(out.split("HARNESS_JOB ", 1)[1].splitlines()[0])


def test_the_router_picks_who_does_a_job_and_the_others_are_not_called(tmp_path, llama, capsys):
    router, asked = llama([{"text": "unused"}], side="**cheap**\nIt is a short summary.")
    cheap, cheap_requests = llama([{"tool": "finish_report", "args": {"report": "summary by cheap"}}])
    spec, cfg = _job(tmp_path, {"router": "triage", "agents": {
        **_profiles(triage=router, cheap=cheap, big=DEAD), "triage": {
            "provider": "llamacpp", "base_url": router, "model_id": "lfm", "use_for": "routing"}}})
    assert job.run(spec, cfg, acquire=_local(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Routed to cheap (llamacpp cheap): It is a short summary." in out and "summary by cheap" in out
    assert _outcome(out)["agent"] == "cheap" and _outcome(out)["model"] == "llamacpp cheap"
    prompt = fake_llama._text(asked[0]["messages"][0]["content"])
    assert "- cheap: cheap work" in prompt and "- big: big work" in prompt and "triage" not in prompt.split("agents:")[1]
    assert "Write the digest." in fake_llama._text(asked[0]["messages"][-1]["content"])
    assert len(cheap_requests) == 1


def test_a_router_that_is_down_or_names_no_one_leaves_it_to_the_top_level_model(tmp_path, llama, capsys):
    top, top_requests = llama([{"tool": "finish_report", "args": {"report": "done by the top level"}}])
    for router in (DEAD, llama([], side="I would rather not say.")[0]):
        spec, cfg = _job(tmp_path / router[-5:], {"router": "triage", "agents": {
            **_profiles(triage=router, other=DEAD), "triage": {"provider": "llamacpp", "base_url": router,
                                                              "model_id": "r", "model_retries": 0}}})
        cfg = replace(cfg, base_url=top)
        assert job.run(spec, cfg, acquire=_local(tmp_path / router[-5:])) == 0
        out = capsys.readouterr().out
        assert "! Not routed:" in out and "done by the top level" in out and _outcome(out)["agent"] is None
    assert len(top_requests) == 2


def test_the_lead_delegates_and_hears_only_the_report(tmp_path, llama, capsys):
    helper, helper_requests = llama([
        {"tool": "shell", "args": {"command": "cat scripts/fetch.py"}},
        {"text": "The script prints: {last_tool}"},
    ])
    lead, lead_requests = llama([
        {"tool": "delegate", "args": {"to": "helper", "task": "Read scripts/fetch.py and say what it prints."}},
        {"tool": "finish_report", "args": {"report": "Helper said: {last_tool}"}},
    ])
    spec, cfg = _job(tmp_path, {"delegate_to": ["helper"], "agents": _profiles(helper=helper)})
    assert job.run(spec, replace(cfg, base_url=lead), acquire=_local(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Helper said: The script prints:" in out and "print('hi')" in out.split("digest report")[1]
    assert "--- delegating to helper (llamacpp helper) ---" in out and "delegated to helper (llamacpp helper)" in out
    assert _outcome(out)["delegated"] == {"helper": 1}

    lead_tools = {t["function"]["name"]: t["function"] for t in lead_requests[0]["tools"]}
    assert "- helper: helper work" in lead_tools["delegate"]["description"]
    assert "delegate tool" in fake_llama._text(lead_requests[0]["messages"][0]["content"])
    helper_tools = {t["function"]["name"] for t in helper_requests[0]["tools"]}
    assert "shell" in helper_tools and not {"delegate", "finish_report", "finish_with_pr"} & helper_tools
    assert "Read scripts/fetch.py" in fake_llama._text(helper_requests[0]["messages"][-1]["content"])
    # The lead's context has the report, not the helper's tool calls.
    assert not any(m.get("tool_calls") and m["tool_calls"][0]["function"]["name"] == "shell"
                   for m in lead_requests[1]["messages"])


def test_a_delegate_that_fails_is_reported_to_the_lead(tmp_path, llama, capsys):
    lead, lead_requests = llama([
        {"tool": "delegate", "args": {"to": "helper", "task": "anything"}},
        {"tool": "delegate", "args": {"to": "nobody", "task": "anything"}},
        {"tool": "finish_report", "args": {"report": "carried on"}},
    ])
    spec, cfg = _job(tmp_path, {"delegate_to": ["helper"], "agents": {"helper": {"provider": "no_such_module:build"}}})
    assert job.run(spec, replace(cfg, base_url=lead), acquire=_local(tmp_path)) == 0
    results = [fake_llama._text(m["content"]) for m in lead_requests[-1]["messages"] if m.get("role") == "tool"]
    assert results[0].startswith("helper failed:") and "no agent 'nobody'" in results[1]


def test_a_tasks_route_is_chosen_once_and_kept(tmp_path, llama, capsys):
    router, asked = llama([], side="big\nIt is hard.")
    cfg = HarnessConfig.load(None, {"router": "triage", "agents": _profiles(triage=router, small=DEAD, big=DEAD)})
    saved = tmp_path / "t.agent.json"
    first, name = agents.lead(cfg, "Rewrite the parser", saved=saved)
    assert (name, first.model_id, first.base_url, first.router) == ("big", "big", DEAD, "")
    again, name = agents.lead(cfg, None, saved=saved)
    assert (name, again.model_id) == ("big", "big") and len(asked) == 1
    assert "as the router chose" in capsys.readouterr().out
    # No router, no task (a resume of a task never routed): the top-level model.
    assert agents.lead(replace(cfg, router=""), "x", saved=tmp_path / "none.json") == (replace(cfg, router=""), "")


def test_a_profile_is_a_whole_model_and_names_are_checked(tmp_path, monkeypatch):
    cfg = HarnessConfig.load(None, {"base_url": "http://local:8080", "model_params": '{"top_k": 20}',
                                    "agents": {"claude": {"model_id": "claude-haiku-5-5", "use_for": "x",
                                                          "delegate_to": ["claude"]}}})
    claude = agents.as_profile(cfg, "claude")
    assert (claude.provider, claude.base_url, claude.model_params, claude.model_id) == (
        "anthropic", "", "", "claude-haiku-5-5")
    assert claude.delegate_to == ("claude",)
    assert agents.as_profile(replace(cfg, delegate_to=("x",), agents={"q": {}}), "q").delegate_to == ("x",)
    for bad, message in (({"agents": {"x": {"egress": "open"}}}, "may not set: egress"),
                         ({"agents": {"x": {"max_tokens": "lots"}}}, "max_tokens"),
                         ({"delegate_to": ("ghost",)}, "delegate_to names no profile: ghost"),
                         ({"agents": {"r": {}}, "router": "r"}, "nothing to pick from"),
                         ({"agents": {"a": {"delegate_to": ["b"]}}}, "[agents.a] delegate_to names no profile")):
        with pytest.raises(ValueError, match=message.replace("[", r"\[")):
            HarnessConfig.load(None, bad)
    with pytest.raises(ValueError, match="may not set: agents"):
        HarnessConfig.load('[agents.x]\nmodel_id = "y"\n')  # a repo may not say where requests go

    # Profiles add up across files and the environment; a --config file layers over the user's.
    (tmp_path / "xdg" / "burst-harness").mkdir(parents=True)
    (tmp_path / "xdg" / "burst-harness" / "config.toml").write_text('[agents.a]\nmodel_id = "a"\n')
    scenario = tmp_path / "scenario.toml"
    scenario.write_text('delegate_to = ["a", "b"]\n[agents.b]\nmodel_id = "b"\n')
    monkeypatch.setenv("HARNESS_AGENTS", '{"c": {"model_id": "c"}}')
    monkeypatch.setenv("HARNESS_CONFIG", str(scenario))
    loaded = HarnessConfig.load(None, user_config=tmp_path / "xdg" / "burst-harness" / "config.toml")
    assert sorted(loaded.agents) == ["a", "b", "c"] and loaded.delegate_to == ("a", "b")


def test_a_router_reply_is_read_leniently():
    names = ("coder", "Reviewer", "scout")
    assert agents.pick("<think>coder? no, scout</think>\n`reviewer`\nbecause", names) == "Reviewer"
    assert agents.pick("I'd go with coder: it writes code.", names) == "coder"
    assert agents.pick("Agent: scout.", names) == "scout"
    assert agents.pick("none of them", names) is None


def test_a_run_that_names_its_model_is_not_routed(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_session", lambda args, body: seen.append(cli._overrides(args)) or 0)
    cli.main(["--router", "triage", "start", "s", "--task", "x"])
    cli.main(["--router", "triage", "--model", "m", "start", "s", "--task", "x"])
    cli.main(["--router", "triage", "--agent", "a", "--delegate-to", "b,c", "start", "s", "--task", "x"])
    assert [o["router"] for o in seen] == ["triage", "", ""]
    assert seen[2]["agent"] == "a" and seen[2]["delegate_to"] == ("b", "c")


def test_a_task_is_routed_once_and_its_lead_delegates(tmp_path, origin, llama, capsys):
    from .conftest import git

    router, asked = llama([], side="coder\nIt is code.")
    scout, scout_requests = llama([{"text": "README.md says hello."}])
    coder, coder_requests = llama([
        {"tool": "delegate", "args": {"to": "scout", "task": "What does README.md say?"}},
        {"tool": "pause_for_review", "args": {"summary": "Scout: {last_tool}", "next_step": "Carry on."}},
        {"text": "Paused."},
    ])
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    scenario = tmp_path / "scenario.toml"
    scenario.write_text(f'provider = "llamacpp"\nbase_url = "{DEAD}"\nrouter = "triage"\n'
                        + "".join(f'[agents.{name}]\nprovider = "llamacpp"\nbase_url = "{url}"\nmodel_id = "{name}"\n'
                                  f'use_for = "{name} work"\n' + extra for name, url, extra in
                                  (("triage", router, ""), ("scout", scout, ""),
                                   ("coder", coder, 'delegate_to = ["scout"]\n'))))  # it delegates when it leads

    assert cli.main(["--config", str(scenario), "--repo", str(work), "start", "t1", "--task", "Fix it."]) == 0
    out = capsys.readouterr().out
    result = json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])
    assert (result["state"], result["agent"], result["model"]) == ("paused", "coder", "llamacpp coder")
    assert result["delegated"]["scout"]["calls"] == 1
    assert "Scout: README.md says hello." in (work / "docs" / "tasks" / "t1.md").read_text()
    assert "- scout: scout work" in fake_llama._text(coder_requests[0]["messages"][0]["content"])

    # A later burst keeps the router's choice, without asking it again.
    assert cli.main(["--config", str(scenario), "--repo", str(work), "resume", "t1"]) == cli.EXIT_NO_TERMINAL_STATE
    assert len(asked) == 1 and "as the router chose" in capsys.readouterr().out
    assert len(coder_requests) > 3 and len(scout_requests) == 1


@pytest.mark.parametrize("scenario", ["front-door", "strong-lead", "small-lead", "local-only"])
def test_the_example_scenarios_load(monkeypatch, scenario):
    examples = Path(__file__).parent.parent / "examples" / "agents"
    monkeypatch.setenv("HARNESS_CONFIG", os.pathsep.join((str(examples / "profiles.toml"), str(examples / f"{scenario}.toml"))))
    cfg = HarnessConfig.load(None)
    lead_cfg, name = agents.lead(cfg, None)
    assert set(lead_cfg.delegate_to) <= set(cfg.agents) and (name == cfg.agent)
