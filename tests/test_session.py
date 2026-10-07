import json
import stat

import pytest

from harness import session
from harness.config import HarnessConfig


def test_names_are_checked():
    assert session.container_name("demo-1") == "burst-session-demo-1"
    for bad in ("", "Demo", "a b", "-x", "x" * 41, "a/b"):
        with pytest.raises(session.SessionError):
            session.container_name(bad)


def test_volumes_are_the_workspace_and_the_home():
    assert session.volumes("demo") == {
        "burst-session-demo-work": "/workspace", "burst-session-demo-home": "/home/agent",
    }


def test_opencode_config_for_llamacpp_points_at_v1():
    cfg = HarnessConfig(provider="llamacpp", base_url="http://llm.lan:8080/", model_id="qwen3",
                        context_window_limit=65536, max_tokens=8192)
    config = session.opencode_config(cfg)
    provider = config["provider"]["harness"]
    assert provider["options"]["baseURL"] == "http://llm.lan:8080/v1"
    assert provider["models"]["qwen3"]["limit"] == {"context": 65536, "output": 8192}
    assert config["model"] == "harness/qwen3" and config["autoupdate"] is False and config["share"] == "disabled"
    assert session.model_endpoint(config) == ("llm.lan:8080",)


def test_opencode_config_for_openai_uses_the_url_as_given():
    config = session.opencode_config(HarnessConfig(provider="openai", base_url="https://api.example.com/v1"))
    assert config["provider"]["harness"]["options"]["baseURL"] == "https://api.example.com/v1"
    assert session.model_endpoint(config) == ("api.example.com",)


def test_no_opencode_config_for_other_providers():
    assert session.opencode_config(HarnessConfig()) is None
    assert session.opencode_config(HarnessConfig(provider="llamacpp")) is None  # no base_url
    assert session.model_endpoint(None) == ()


def test_output_limit_never_exceeds_half_the_context():
    config = session.opencode_config(HarnessConfig(provider="llamacpp", base_url="http://l:1", context_window_limit=8192))
    assert config["provider"]["harness"]["models"][HarnessConfig().model_id]["limit"]["output"] == 4096


def test_record_is_private_and_round_trips(tmp_path):
    cfg = HarnessConfig(state_dir=str(tmp_path))
    record = {"name": "demo", "bind": "0.0.0.0", "port": 4100, "password": "pw"}
    session.save_record(cfg, "demo", record)
    path = session.record_path(cfg, "demo")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert session.load_record(cfg, "demo") == record
    assert session.load_record(cfg, "other") == {}
    shown = session.describe(record)
    assert "http://127.0.0.1:4100/" in shown and "other addresses" in shown and "pw" in shown


def test_describe_shows_the_terminal_when_there_is_one():
    record = {"bind": "127.0.0.1", "port": 4100, "password": "pw", "terminal": {"port": 4101, "token": "tok"}}
    shown = session.describe(record)
    assert "http://127.0.0.1:4101/" in shown and "tok" in shown
    assert "terminal" not in session.describe({"bind": "127.0.0.1", "port": 4100, "password": "pw"})
