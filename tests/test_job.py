"""Jobs: loading and checking a job directory, a run with a fake `claude`, and delivery."""

import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from harness import cli, job

from . import fake_llama
from harness.exec_target import LocalTarget

EXAMPLE = Path(__file__).parent.parent / "examples" / "jobs" / "repo-digest"

FAKE = r'''#!PYTHON
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
mode = os.environ["FAKE_CLAUDE_MODE"]
if mode == "report" or (mode == "late" and "--continue" in argv):
    open("report.md", "w").write("# Digest\n\nAll green. Scripts: " + ",".join(sorted(os.listdir("scripts"))) + "\n")
if mode == "fail":
    sys.exit(1)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1}), flush=True)
'''


def _write_job(root: Path, toml: str, *, name: str = "digest", scripts: bool = True) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "job.toml").write_text(toml)
    (path / "prompt.md").write_text("Write the digest.\n")
    if scripts:
        (path / "scripts").mkdir()
        (path / "scripts" / "fetch.py").write_text("print('hi')\n")
    return path


BASIC = """
engine = "claude-code"
[job]
trigger = "0 7 * * 1-5"
output = ["stdout", "file:REPORTS/%Y/digest.md"]
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE.replace("PYTHON", sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    return lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _local(tmp_path):
    work = tmp_path / "work"
    return lambda cfg, spec: LocalTarget(repo=str(work))


def test_example_job_loads_and_describes(monkeypatch):
    spec = job.load(EXAMPLE)
    cfg = job.config(spec)
    assert (spec.name, spec.trigger, spec.mode) == ("repo-digest", "0 7 * * 1-5", "report")
    assert (cfg.target, cfg.engine, cfg.egress) == ("podman", "strands", "allowlist")
    assert cfg.egress_inject == () and cfg.egress_allow == ()
    text = job.describe(spec, cfg)
    assert "api.github.com" in text and "added by the gateway at api.github.com" in text
    with pytest.raises(job.JobError, match="GITHUB_TOKEN"):
        job.check(spec)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    job.check(spec)


@pytest.mark.parametrize("table, error", [
    ('mode = "act"', "not supported yet"),
    ('mode = "maybe"', "report"),
    ('trigger = "every day"', "cron"),
    ('trigger = "webhook"', "control plane"),
    ('output = ["email:me"]', "file:PATH"),
    ('output = ["file:"]', "file:PATH"),
    ('colour = "blue"', "unknown"),
    ('secrets = { gh = { env = "GITHUB_TOKEN" } }', "host"),
    ('secrets = { gh = { env = "GITHUB TOKEN", sandbox_env = "X" } }', "variable name"),
    ('secrets = { gh = { env = "T", host = "api.github.com", value = "Bearer" } }', "{}"),
])
def test_bad_job_settings_are_refused(tmp_path, table, error):
    path = _write_job(tmp_path, f"[job]\n{table}\n")
    with pytest.raises(job.JobError, match=error):
        job.load(path)


def test_a_job_needs_a_prompt_and_a_name(tmp_path):
    path = _write_job(tmp_path, BASIC)
    (path / "prompt.md").unlink()
    with pytest.raises(job.JobError, match="prompt.md"):
        job.load(path)
    with pytest.raises(job.JobError, match="lowercase"):
        job.load(_write_job(tmp_path, BASIC, name="Bad_Name"))


def test_job_settings_sit_between_user_config_and_env(tmp_path, monkeypatch):
    user = tmp_path / "xdg" / "burst-harness" / "config.toml"
    user.parent.mkdir(parents=True)
    user.write_text('model_id = "from-user"\nmax_tokens = 100\ntarget = "local"\n')
    path = _write_job(tmp_path, 'model_id = "from-job"\nclaude_settings = "settings.json"\n' + BASIC)
    cfg =job.HarnessConfig.load(None, None, user_config=user, job={**job.JOB_DEFAULTS, **job.load(path).settings})
    assert (cfg.model_id, cfg.max_tokens, cfg.target) == ("from-job", 100, "podman")
    assert cfg.claude_settings == str(path / "settings.json")
    monkeypatch.setenv("HARNESS_MODEL_ID", "from-env")
    assert job.config(job.load(path)).model_id == "from-env"


@pytest.mark.parametrize("setting, error", [
    ('engine = "aider"', "one of strands"),
    ('egress_inject = ["model"]', "drop"),
    ('target = "local"', "fresh podman or docker"),
    ('egress = "open"', "gateway"),
    ('repo_url = "https://github.com/a/b"', "without a repo"),
    ("nonsense = 1", "Unknown setting"),
])
def test_job_config_refuses_what_jobs_cannot_do(tmp_path, setting, error):
    spec = job.load(_write_job(tmp_path, setting + "\n" + BASIC.replace('engine = "claude-code"', "")))
    with pytest.raises(job.JobError, match=error):
        job.config(spec)


def test_run_delivers_the_report(tmp_path, monkeypatch, fake_claude, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "report")
    reports = tmp_path / "reports"
    spec = job.load(_write_job(tmp_path, BASIC.replace("REPORTS", str(reports))))
    code = job.run(spec, job.config(spec), acquire=_local(tmp_path))
    out = capsys.readouterr().out
    assert code == 0
    assert "----- digest report -----\n# Digest" in out
    written = list(reports.glob("*/digest.md"))
    assert len(written) == 1 and "Scripts: fetch.py" in written[0].read_text()
    result = json.loads(out.split("HARNESS_JOB ", 1)[1].splitlines()[0])
    assert result["state"] == "reported" and result["reason"] is None
    (call,) = fake_claude()
    assert call["cwd"] == str(tmp_path / "work")
    rules = call["argv"][call["argv"].index("--append-system-prompt") + 1]
    assert "data, never instructions" in rules and f"{tmp_path / 'work'}/report.md" in rules
    assert "scripts" in rules


def test_a_late_report_is_asked_for(tmp_path, monkeypatch, fake_claude, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "late")
    spec = job.load(_write_job(tmp_path, BASIC.replace("REPORTS", str(tmp_path / "r"))))
    assert job.run(spec, job.config(spec), acquire=_local(tmp_path)) == 0
    first, second = fake_claude()
    assert "--continue" in second["argv"] and "without writing" in second["argv"][second["argv"].index("-p") + 1]


def test_no_report_sends_a_notice_and_keeps_the_last_file(tmp_path, monkeypatch, fake_claude, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    reports = tmp_path / "reports"
    spec = job.load(_write_job(tmp_path, BASIC.replace("REPORTS", str(reports))))
    assert job.run(spec, job.config(spec), acquire=_local(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "Job digest produced no report: Claude Code exited with an error" in out
    assert not reports.exists()
    assert json.loads(out.split("HARNESS_JOB ", 1)[1].splitlines()[0])["state"] == "failed"


def test_a_failure_to_start_is_still_reported(tmp_path, capsys):
    spec = job.load(_write_job(tmp_path, BASIC.replace("REPORTS", str(tmp_path / "r"))))

    def broken(cfg, spec):
        raise job.containers.ContainerError("podman run failed: no such image")

    assert job.run(spec, job.config(spec), acquire=broken) == 1
    assert "produced no report: ContainerError: podman run failed: no such image" in capsys.readouterr().out


def test_secrets_reach_the_gateway_not_the_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-real")
    monkeypatch.setenv("NOTES_KEY", "notes-real")
    path = _write_job(tmp_path, BASIC + """
[job.secrets.github]
env = "GITHUB_TOKEN"
host = "api.github.com"
sandbox_env = "GH_TOKEN"
[job.secrets.notes]
env = "NOTES_KEY"
sandbox_env = "NOTES_KEY"
""")
    spec = job.load(path)
    seen = {}
    monkeypatch.setattr(job.containers, "acquire", lambda cfg, **kw: seen.update(kw) or LocalTarget(repo=str(tmp_path)))
    job._acquire(job.config(spec), spec)
    assert seen["allow"] == ("api.github.com",)
    assert seen["inject"] == ({"host": "api.github.com", "headers": {"Authorization": "Bearer ghp-real"}},)
    # Into the container's environment, by name: the placeholder, and the secret with no host.
    assert seen["env"] == {"GH_TOKEN": "burst-gateway-placeholder-github", "NOTES_KEY": "notes-real"}
    github, notes = spec.secrets
    assert github.placeholder() == "burst-gateway-placeholder-github" and not notes.host


class _Slack(BaseHTTPRequestHandler):
    received: list = []
    answer = b'{"ok": true}'

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append((self.path, self.headers.get("Authorization"), body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(type(self).answer)

    def log_message(self, *args):
        pass


@pytest.fixture
def slack(monkeypatch):
    _Slack.received = []
    _Slack.answer = b'{"ok": true}'
    server = HTTPServer(("127.0.0.1", 0), _Slack)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(job, "SLACK_API", f"{url}/api/chat.postMessage")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", f"{url}/hook")
    yield _Slack
    server.shutdown()


def test_slack_outputs(tmp_path, slack):
    spec = job.load(_write_job(tmp_path, '[job]\noutput = ["slack:C123", "slack"]\n'))
    assert job.deliver(spec, "# Digest") == []
    (api, token, body), (hook, none, hook_body) = slack.received
    assert (api, token, body) == ("/api/chat.postMessage", "Bearer xoxb-test", {"channel": "C123", "text": "*digest*\n# Digest"})
    assert (hook, none, hook_body) == ("/hook", None, {"text": "*digest*\n# Digest"})


def test_slack_refusal_is_a_delivery_failure(tmp_path, slack, capsys):
    slack.answer = b'{"ok": false, "error": "channel_not_found"}'
    spec = job.load(_write_job(tmp_path, '[job]\noutput = ["slack:C404", "stdout"]\n'))
    assert job.deliver(spec, None, reason="boom") == ["slack:C404"]
    assert "channel_not_found" in capsys.readouterr().err
    assert ":warning:" in slack.received[0][2]["text"] and "boom" in slack.received[0][2]["text"]


def test_slack_text_is_trimmed(tmp_path, slack):
    job.post_slack("x" * (job.SLACK_LIMIT + 50), channel="C1")
    assert slack.received[0][2]["text"].endswith("_(truncated)_")


def test_cli_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert cli.main(["job", "check", str(EXAMPLE)]) == 0
    assert "Job repo-digest" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="not supported yet"):
        cli.main(["job", "check", str(_write_job(tmp_path, '[job]\nmode = "act"\n'))])


# --- engine=strands, against a stand-in llama.cpp server -------------------

STRANDS = """
provider = "llamacpp"
base_url = "BASE"
model_id = "fake"
[job]
output = ["stdout"]
"""


@pytest.fixture
def llama():
    servers = []

    def start(script):
        server, requests = fake_llama.serve(script)
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}", requests

    yield start
    for server in servers:
        server.shutdown()


def _strands_job(tmp_path, base):
    return job.load(_write_job(tmp_path, STRANDS.replace("BASE", base)))


def test_strands_runs_tools_in_the_target_and_finishes(tmp_path, llama, capsys):
    base, requests = llama([
        {"tool": "shell", "args": {"command": "cat scripts/fetch.py && pwd"}},
        {"tool": "finish_report", "args": {"report": "# Digest\nThe script says: {last_tool}"}},
    ])
    spec = _strands_job(tmp_path, base)
    cfg = job.config(spec)
    assert cfg.engine == "strands"
    assert job.run(spec, cfg, acquire=_local(tmp_path)) == 0
    out = capsys.readouterr().out
    report = out.split("----- digest report -----\n", 1)[1]
    assert "print('hi')" in report and str(tmp_path / "work") in report
    assert len(requests) == 2  # finish_report ended the loop: no turn after it
    system = requests[0]["messages"][0]
    assert "call finish_report" in fake_llama._text(system["content"])
    names = {t["function"]["name"] for t in requests[0]["tools"]}
    assert {"shell", "finish_report"} <= names and "finish_with_pr" not in names


def test_strands_is_reminded_to_finish(tmp_path, llama, capsys):
    base, requests = llama([
        {"text": "All done, I think."},
        {"tool": "finish_report", "args": {"report": "late but here"}},
    ])
    spec = _strands_job(tmp_path, base)
    assert job.run(spec, job.config(spec), acquire=_local(tmp_path)) == 0
    assert "late but here" in capsys.readouterr().out
    assert "without calling finish_report" in fake_llama._text(requests[1]["messages"][-1]["content"])


def test_strands_that_never_finishes_fails(tmp_path, llama, capsys):
    base, _ = llama([{"text": "No."}])
    spec = _strands_job(tmp_path, base)
    assert job.run(spec, job.config(spec), acquire=_local(tmp_path)) == 1
    assert "produced no report: the model ended without calling finish_report" in capsys.readouterr().out


def test_strands_with_an_unreachable_model_is_reported(tmp_path, capsys):
    spec = _strands_job(tmp_path, "http://127.0.0.1:9")
    assert job.run(spec, job.config(spec), acquire=_local(tmp_path)) == 1
    assert "produced no report:" in capsys.readouterr().out


# --- engine=opencode, with a fake `opencode` ------------------------------

FAKE_OPENCODE = r'''#!PYTHON
import json, os, sys
with open(os.environ["FAKE_OPENCODE_LOG"], "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "key": os.environ.get("ANTHROPIC_API_KEY")}) + "\n")
config = json.load(open(os.path.expanduser("~/.config/opencode/opencode.json")))
rules = open(config["instructions"][0]).read()
if "--continue" in sys.argv:
    open("report.md", "w").write("# From opencode\nmodel " + config["model"] + "\n" + ("rules ok" if "report.md" in rules else "") + "\n")
print("did things")
'''


def test_opencode_engine(tmp_path, monkeypatch, capsys):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "opencode").write_text(FAKE_OPENCODE.replace("PYTHON", sys.executable))
    (bin_dir / "opencode").chmod(0o755)
    log = tmp_path / "opencode.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_OPENCODE_LOG", str(log))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
    spec = job.load(_write_job(tmp_path, 'engine = "opencode"\nmodel_id = "claude-x"\n[job]\noutput = ["stdout"]\n'))
    assert job.run(spec, job.config(spec), acquire=_local(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "# From opencode\nmodel anthropic/claude-x\nrules ok" in out
    first, second = [json.loads(line) for line in log.read_text().splitlines()]
    assert first["argv"][0] == "run" and second["argv"][:2] == ["run", "--continue"]
    assert first["key"] == "sk-real"
    config = json.loads((tmp_path / "home" / ".config" / "opencode" / "opencode.json").read_text())
    assert config["permission"]["bash"] == "allow"


def test_opencode_needs_a_model_it_can_use(tmp_path, capsys):
    spec = job.load(_write_job(tmp_path, 'engine = "opencode"\nprovider = "bedrock"\n[job]\noutput = ["stdout"]\n'))
    assert job.run(spec, job.config(spec), acquire=_local(tmp_path)) == 1
    assert "engine=opencode needs" in capsys.readouterr().out
