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


def test_output_limit_never_exceeds_half_the_context(monkeypatch):
    monkeypatch.setattr(session, "served_model", lambda base: None)
    config = session.opencode_config(HarnessConfig(provider="llamacpp", base_url="http://l:1", context_window_limit=8192))
    assert config["provider"]["harness"]["models"]["llama.cpp"]["limit"]["output"] == 4096


def test_llamacpp_without_a_model_uses_what_the_server_serves(monkeypatch):
    asked = []
    monkeypatch.setattr(session, "served_model", lambda base: asked.append(base) or "/models/Qwen3-Coder-30B.gguf")
    config = session.opencode_config(HarnessConfig(provider="llamacpp", base_url="http://l:1"))
    assert asked == ["http://l:1/v1"]
    assert config["model"] == "harness/Qwen3-Coder-30B"
    assert config["provider"]["harness"]["models"]["Qwen3-Coder-30B"]["name"] == "Qwen3-Coder-30B"
    # A model that was chosen is used as given, without asking.
    config = session.opencode_config(HarnessConfig(provider="llamacpp", base_url="http://l:1", model_id="qwen3"))
    assert config["model"] == "harness/qwen3" and len(asked) == 1


def test_served_model_asks_the_server_and_reaches_the_host_as_loopback():
    import http.server, threading
    class Models(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"data": [{"id": "qwen3"}]}).encode() if self.path == "/v1/models" else b"{}"
            self.send_response(200 if self.path == "/v1/models" else 404); self.end_headers(); self.wfile.write(body)
        def log_message(self, *args): pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Models)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        assert session.served_model(f"http://host.containers.internal:{port}/v1") == "qwen3"
        assert session.served_model(f"http://127.0.0.1:{port}/nothing") is None
    finally:
        server.shutdown()


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


@pytest.mark.parametrize("text,expected", [("90", 90), ("45s", 45), ("90m", 5400), ("2h", 7200), ("1d", 86400), ("0", 0)])
def test_seconds(text, expected):
    assert session.seconds(text) == expected


def test_seconds_rejects_nonsense():
    for bad in ("", "soon", "2 hours", "-5", "1.5h"):
        with pytest.raises(session.SessionError):
            session.seconds(bad)


def test_anthropic_key_reaches_opencode_as_itself_or_as_a_placeholder(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-key")
    cfg = HarnessConfig(model_id="claude-x")
    assert session.model_env(cfg) == {"ANTHROPIC_API_KEY": "real-key"}
    config = session.opencode_config(cfg)
    assert config["model"] == "anthropic/claude-x" and "provider" not in config
    assert session.model_endpoint(config) == ()
    held = session.model_env(HarnessConfig(egress_inject=("model",)))
    assert "real-key" not in held.values() and "placeholder" in held["ANTHROPIC_API_KEY"]
    assert session.model_env(HarnessConfig(provider="llamacpp", base_url="http://l:1")) == {}


def test_describe_shows_when_it_stops():
    record = {"bind": "127.0.0.1", "port": 1, "password": "pw", "idle": 7200, "lifetime": 0}
    assert "after idle 7200s" in session.describe(record)


def test_urls_and_describe_for_a_claude_code_session():
    record = {"bind": "127.0.0.1", "port": 4200, "password": "pw", "agent": "claude-code",
              "terminal": {"port": 4200, "token": "tok", "path": "claude"}, "allow": ["example.com"]}
    assert session.urls(record) == {"terminal": "http://127.0.0.1:4200/claude", "ui": "http://127.0.0.1:4200/claude"}
    shown = session.describe(record)
    assert "Claude Code" in shown and "tok" in shown and "pw" not in shown and "example.com" in shown


def test_claude_env_swaps_in_placeholders_when_the_gateway_holds_the_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example")
    assert session.claude_env(HarnessConfig())["ANTHROPIC_API_KEY"] == "real-key"
    held = session.claude_env(HarnessConfig(egress_inject=("model",)))
    assert "placeholder" in held["ANTHROPIC_API_KEY"] and held["ANTHROPIC_BASE_URL"] == "https://proxy.example"


def test_add_allow_keeps_entries_once(tmp_path):
    cfg = HarnessConfig(state_dir=str(tmp_path))
    with pytest.raises(session.SessionError):
        session.add_allow(cfg, "nope", "example.com")
    session.save_record(cfg, "box", {"name": "box", "allow": []})
    assert session.add_allow(cfg, "box", "example.com") and not session.add_allow(cfg, "box", "example.com")
    assert session.load_record(cfg, "box")["allow"] == ["example.com"]


def test_the_ui_opens_on_the_latest_opencode_session_in_the_directory_or_a_new_one():
    calls = []
    listed = [{"id": "ses_old", "directory": "/workspace", "time": {"updated": 1}},
              {"id": "ses_new", "directory": "/workspace", "time": {"updated": 5}},
              {"id": "ses_other", "directory": "/workspace/elsewhere", "time": {"updated": 9}}]

    def api(method, path, body):
        calls.append((method, path, body))
        return listed if method == "GET" else {"id": "ses_made"}

    assert session.opencode_session(api, "/workspace", "demo") == "ses_new"
    assert calls == [("GET", "/session?directory=/workspace&roots=true", None)]
    listed.clear()
    assert session.opencode_session(api, "/workspace", "demo") == "ses_made"
    assert calls[-1] == ("POST", "/session?directory=/workspace", {"title": "demo"})


def test_the_link_is_the_web_apps_path_to_the_session():
    # As opencode's app encodes a directory: base64url, unpadded.
    assert session.opencode_link("/workspace", "ses_1") == "L3dvcmtzcGFjZQ/session/ses_1"
    assert session.opencode_link("/workspace/repo", "ses_1") == "L3dvcmtzcGFjZS9yZXBv/session/ses_1"


def test_opencode_api_logs_in_and_a_refusal_is_a_note():
    import base64
    import http.server
    import threading

    seen = []

    class Opencode(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen.append((self.command, self.path, self.headers.get("Authorization"),
                         self.rfile.read(length).decode() if length else None))
            status, data = (500, b"{}") if "broken" in self.path else (200, json.dumps(
                {"id": "ses_9"} if self.command == "POST" else []).encode())
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        do_GET = do_POST = _answer

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Opencode)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        api = session._opencode_api(f"http://127.0.0.1:{server.server_address[1]}", "pw")
        notes = []
        assert session.open_in(api, "/workspace", "demo", notes) == "L3dvcmtzcGFjZQ/session/ses_9" and notes == []
        assert session.open_in(api, "/broken", "demo", notes) == "" and "add it as a project in the UI" in notes[0]
    finally:
        server.shutdown()
    auth = "Basic " + base64.b64encode(b"opencode:pw").decode()
    assert seen[:2] == [("GET", "/session?directory=/workspace&roots=true", auth, None),
                        ("POST", "/session?directory=/workspace", auth, '{"title": "demo"}')]


def test_urls_open_on_the_session_when_there_is_one():
    record = {"bind": "127.0.0.1", "port": 4100, "password": "pw", "link": "L3dvcmtzcGFjZQ/session/ses_1"}
    assert session.urls(record)["ui"] == "http://127.0.0.1:4100/L3dvcmtzcGFjZQ/session/ses_1"
    assert session.urls({**record, "link": ""})["ui"] == "http://127.0.0.1:4100/"
