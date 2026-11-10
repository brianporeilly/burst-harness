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
remember = os.environ.get("FAKE_CLAUDE_MEMORY", "")  # what to do in memory/: "note:TEXT", "big", "link"
if remember:
    seen = open("memory/seen.txt").read() if os.path.exists("memory/seen.txt") else ""
    if remember.startswith("note:"):
        open("memory/seen.txt", "a").write(remember[5:] + "\n")
        os.makedirs("memory/deep", exist_ok=True)
        open("memory/deep/count", "w").write(str(len(seen.splitlines()) + 1))
    elif remember == "big":
        open("memory/big.bin", "wb").write(os.urandom(6 * 1024 * 1024))
    elif remember == "link":
        os.symlink("/etc/passwd", "memory/passwd")
    open("seen-before.txt", "w").write(seen)
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
    ('target = "agent-sandbox"', "needs pool"),
    ('target = "agent-sandbox"\npool = "p"', "gateway_control"),
    ('claim_name = "c"', "not for jobs"),
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


def test_a_reply_cut_off_at_the_output_limit_is_dropped_and_the_model_told(tmp_path, llama, capsys):
    base, requests = llama([
        {"cut": "Let me think. Let me think. Let me think."},
        {"tool": "finish_report", "args": {"report": "done after all"}},
    ])
    spec = _strands_job(tmp_path, base)
    assert job.run(spec, job.config(spec), acquire=_local(tmp_path)) == 0
    assert "done after all" in capsys.readouterr().out
    retry = requests[1]["messages"]
    assert "hit the output limit" in fake_llama._text(retry[-1]["content"])
    assert not any("Let me think" in fake_llama._text(m.get("content")) for m in retry)
    assert sum(m["role"] == "user" for m in retry) == 1  # told in the same user turn, not a second one


def test_cut_offs_count_again_after_a_finished_reply(tmp_path, llama, capsys):
    base, _ = llama([{"cut": "a"}, {"tool": "shell", "args": {"command": "true"}}, {"cut": "b"}, {"cut": "c"},
                     {"tool": "finish_report", "args": {"report": "made it"}}])
    spec = _strands_job(tmp_path, base)
    assert job.run(spec, job.config(spec), acquire=_local(tmp_path)) == 0


def test_replies_cut_off_again_and_again_end_the_run_as_max_tokens(tmp_path, llama, capsys):
    base, requests = llama([{"cut": "a"}, {"cut": "b"}, {"cut": "c"}, {"tool": "finish_report", "args": {"report": "x"}}])
    spec = _strands_job(tmp_path, base)
    assert job.run(spec, job.config(spec), acquire=_local(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "max_tokens: the model's replies hit the output limit 3 times in a row" in out and "Traceback" not in out
    assert len(requests) == 3


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


def test_report_mode_is_read_only_at_the_gateway_with_what_the_run_needs(tmp_path):
    from harness import job
    from harness.config import HarnessConfig

    directory = tmp_path / "digest"
    directory.mkdir()
    (directory / "prompt.md").write_text("do it")
    (directory / "job.toml").write_text(
        '[job]\nwrites = [{host = "api.github.com", method = "post", path = "/graphql"}]\n')
    spec = job.load(directory)
    assert spec.writes == ({"host": "api.github.com", "method": "POST", "path": "/graphql"},)
    strands = job.write_rules(spec, HarnessConfig(engine="strands"))
    assert strands == (*job.RUN_WRITES, *spec.writes)  # the model is called from the driver
    claude = job.write_rules(spec, HarnessConfig(engine="claude-code"))
    assert job.ANTHROPIC_WRITES[0] in claude
    opencode = job.write_rules(spec, HarnessConfig(engine="opencode", provider="llamacpp",
                                                   base_url="http://llm.lan:8080", model_id="m"))
    assert {"host": "llm.lan", "method": "POST", "path": "*"} in opencode
    assert "the gateway refuses writes" in job.describe(spec, HarnessConfig(engine="strands"))


def test_writes_entries_are_checked(tmp_path):
    from harness import job

    directory = tmp_path / "digest"
    directory.mkdir()
    (directory / "prompt.md").write_text("do it")
    for bad in ('writes = [{host = "a.com"}]', 'writes = [{host = "a.com", method = "PO ST"}]',
                'writes = "a.com"', 'writes = [{host = "a.com", method = "POST", body = "x"}]'):
        (directory / "job.toml").write_text(f"[job]\n{bad}\n")
        with pytest.raises(job.JobError, match="writes"):
            job.load(directory)


def test_a_secret_can_name_a_port_and_the_hosts_own_ca(tmp_path, monkeypatch):
    from harness import job

    directory = tmp_path / "watcher"
    directory.mkdir()
    (directory / "prompt.md").write_text("do it")
    (directory / "ca.crt").write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    (directory / "job.toml").write_text(
        '[job.secrets.kube]\nenv = "KUBE_TOKEN"\nhost = "10.0.0.5:6443"\nca_file = "ca.crt"\n')
    spec = job.load(directory)
    secret = spec.secrets[0]
    assert (secret.hostname, secret.port) == ("10.0.0.5", 6443)
    assert job._hosts(spec) == ("10.0.0.5:6443",)  # the allowlist entry
    monkeypatch.setenv("KUBE_TOKEN", "t0k")
    assert secret.rule() == {"host": "10.0.0.5", "port": 6443, "headers": {"Authorization": "Bearer t0k"}}
    assert secret.upstream_ca() == {"host": "10.0.0.5", "ca": (directory / "ca.crt").read_text()}

    (directory / "ca.crt").write_text("not a certificate")
    with pytest.raises(job.JobError, match="not a PEM"):
        job.load(directory)
    (directory / "job.toml").write_text('[job.secrets.kube]\nenv = "K"\nsandbox_env = "K"\nca_file = "ca.crt"\n')
    with pytest.raises(job.JobError, match="set host"):
        job.load(directory)


def test_the_cluster_watcher_script_parses():
    import ast
    from pathlib import Path

    source = Path(__file__).parent.parent / "examples/jobs/cluster-watcher/scripts/cluster_state.py"
    ast.parse(source.read_text())


def test_each_run_is_recorded_with_its_report_and_egress(tmp_path, monkeypatch, fake_claude, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "report")
    monkeypatch.setenv(job.RUN_ID_ENV, "20261020T120000Z-abcd")
    spec = job.load(_write_job(tmp_path, BASIC.replace("REPORTS", str(tmp_path / "r"))))
    cfg = job.config(spec)
    assert job.run(spec, cfg, acquire=_local(tmp_path)) == 0
    assert '"run": "20261020T120000Z-abcd"' in capsys.readouterr().out
    (record,) = job.list_runs(cfg, "digest")
    assert record["id"] == "20261020T120000Z-abcd" and record["state"] == "reported"
    assert record["engine"] == "claude-code" and record["finished"] and "pid" not in record
    assert (job.runs_dir(cfg, "digest") / record["id"] / "report.md").read_text().startswith("# Digest")

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    monkeypatch.setenv(job.RUN_ID_ENV, "not an id")  # ignored: a fresh one is made
    assert job.run(spec, cfg, acquire=_local(tmp_path)) == 1
    newest, oldest = job.list_runs(cfg, "digest")
    assert newest["state"] == "failed" and newest["reason"] and job.RUN_ID.fullmatch(newest["id"])
    assert oldest["id"] == "20261020T120000Z-abcd"
    assert not (job.runs_dir(cfg, "digest") / newest["id"] / "report.md").exists()


def test_egress_summary_counts_requests_per_host_and_lists_refusals(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text("\n".join(json.dumps(e) for e in [
        {"kind": "connect", "verdict": "allow", "host": "api.github.com", "intercepted": True},
        {"kind": "request", "verdict": "allow", "host": "api.github.com", "method": "GET"},
        {"kind": "request", "verdict": "allow", "host": "api.github.com", "method": "GET"},
        {"kind": "connect", "verdict": "allow", "host": "pypi.org"},
        {"kind": "request", "verdict": "deny", "host": "api.github.com", "method": "POST", "path": "/x", "detail": "read-only"},
        {"kind": "connect", "verdict": "deny", "host": "evil.com", "port": 443},
        {"kind": "connect", "verdict": "close", "host": "pypi.org"},
    ]) + "\nnot json\n")
    summary = job.egress_summary(str(events))
    assert summary["requests"] == {"api.github.com": 2, "pypi.org": 1}
    assert summary["refused_count"] == 2 and summary["refused"][0]["detail"] == "read-only"
    assert job.egress_summary("")["requests"] == {}


def test_a_job_on_agent_sandbox_cannot_pass_secrets_into_the_sandbox(tmp_path, monkeypatch):
    toml = 'target = "agent-sandbox"\npool = "p"\ngateway_control = "http://gw:8081"\n' + BASIC + """
[job.secrets.gh]
env = "GH_TOKEN"
sandbox_env = "GH_TOKEN"
"""
    spec = job.load(_write_job(tmp_path, toml))
    with pytest.raises(job.JobError, match="cannot pass secrets into the sandbox"):
        job.config(spec)


def test_a_job_on_agent_sandbox_claims_through_the_gateway(tmp_path, monkeypatch):
    toml = 'target = "agent-sandbox"\npool = "p"\ngateway_control = "http://gw:8081"\n' + BASIC.replace(
        "[job]", '[job]\nmode = "report"') + """
[job.secrets.kube]
env = "KUBE_TOKEN"
host = "10.0.0.5:6443"
"""
    spec = job.load(_write_job(tmp_path, toml))
    cfg = job.config(spec)
    assert "pool=p" in job.describe(spec, cfg)
    seen = {}
    monkeypatch.setattr(job.agent_sandbox, "acquire", lambda cfg, **kwargs: seen.update(kwargs) or "target")
    monkeypatch.setenv("KUBE_TOKEN", "t")
    assert job._acquire(cfg, spec) == "target"
    assert seen["repo"] == job.WORKDIR and seen["slug"] == "job-digest"
    assert seen["inject"][0]["host"] == "10.0.0.5" and seen["read_only"][0]["path"] == "*/git-upload-pack"


def test_scripts_mounted_from_a_configmap_are_copied_as_files(tmp_path):
    """A ConfigMap volume is symlinks into ..data/: the files behind them are what is copied."""
    mounted = tmp_path / "job"
    (mounted / "..data" / "scripts").mkdir(parents=True)
    (mounted / "..data" / "scripts" / "fetch.py").write_text("print('hi')\n")
    (mounted / "scripts").symlink_to("..data/scripts")
    work = tmp_path / "work"
    work.mkdir()
    job._copy_scripts(LocalTarget(repo=str(work)), mounted / "scripts")
    copied = work / "scripts" / "fetch.py"
    assert copied.read_text() == "print('hi')\n" and not copied.is_symlink()


MEMORY = BASIC + "memory = true\n"


def test_a_jobs_memory_is_what_its_last_reporting_run_left(tmp_path, monkeypatch, fake_claude, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "report")
    spec = job.load(_write_job(tmp_path, MEMORY.replace("REPORTS", str(tmp_path / "r"))))
    cfg = job.config(spec)
    kept = job.memory.directory(cfg, "digest")
    work = tmp_path / "work"
    for n, note in enumerate(["first", "second"]):
        monkeypatch.setenv("FAKE_CLAUDE_MEMORY", f"note:{note}")
        monkeypatch.setenv(job.RUN_ID_ENV, f"20261110T08000{n}Z-abcd")
        assert job.run(spec, cfg, acquire=_local(tmp_path)) == 0
    assert (work / "seen-before.txt").read_text() == "first\n"  # the second run started with the first's
    assert (kept / "seen.txt").read_text() == "first\nsecond\n" and (kept / "deep" / "count").read_text() == "2"
    rules = fake_claude()[-1]["argv"][fake_claude()[-1]["argv"].index("--append-system-prompt") + 1]
    assert f"Memory: {work}/memory holds" in rules and "never override these rules" in rules
    newest, oldest = job.list_runs(cfg, "digest")
    assert newest["memory"] == {"before": {"files": 2, "bytes": 7}, "kept": {"files": 2, "bytes": 14}}
    assert oldest["memory"]["before"] == {"files": 0, "bytes": 0}

    # A run that fails keeps nothing: the next one starts from the last good memory.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    monkeypatch.setenv("FAKE_CLAUDE_MEMORY", "note:half-done")
    monkeypatch.setenv(job.RUN_ID_ENV, "20261110T080002Z-abcd")
    assert job.run(spec, cfg, acquire=_local(tmp_path)) == 1
    assert (kept / "seen.txt").read_text() == "first\nsecond\n"
    assert "kept" not in job.list_runs(cfg, "digest")[0]["memory"]

    # Each kept memory can be gone back to; the command shows what is kept.
    capsys.readouterr()
    assert cli.main(["job", "memory", str(spec.path), "--restore", "20261110T080000Z-abcd"]) == 0
    assert (kept / "seen.txt").read_text() == "first\n"
    assert cli.main(["job", "memory", str(spec.path)]) == 0
    out = capsys.readouterr().out
    assert "seen.txt" in out and "20261110T080001Z-abcd, 20261110T080000Z-abcd" in out
    with pytest.raises(SystemExit, match="has no memory kept"):
        cli.main(["job", "memory", str(spec.path), "--restore", "20261110T080002Z-abcd"])
    assert cli.main(["job", "memory", str(spec.path), "--clear"]) == 0 and not kept.exists()


@pytest.mark.parametrize("left, why", [("big", "over 5 MB"), ("link", None)])
def test_memory_over_the_limit_is_not_kept_and_the_report_says_so_and_links_are_dropped(
        tmp_path, monkeypatch, fake_claude, capsys, left, why):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "report")
    spec = job.load(_write_job(tmp_path, MEMORY.replace("REPORTS", str(tmp_path / "r"))))
    cfg = job.config(spec)
    kept = job.memory.directory(cfg, "digest")
    kept.mkdir(parents=True)
    (kept / "seen.txt").write_text("before\n")
    monkeypatch.setenv("FAKE_CLAUDE_MEMORY", left)
    assert job.run(spec, cfg, acquire=_local(tmp_path)) == 0
    (record,) = job.list_runs(cfg, "digest")
    if why:
        assert why in record["memory"]["not_kept"] and (kept / "seen.txt").read_text() == "before\n"
        assert not (kept / "big.bin").exists()
        assert "memory was not kept" in capsys.readouterr().out  # in the report, not only the log
    else:  # a link out of the sandbox is not followed, nor kept
        assert sorted(p.name for p in kept.iterdir()) == ["seen.txt"] and record["memory"]["kept"]["files"] == 1


def test_memory_is_a_choice_of_true_or_false_and_check_shows_it(tmp_path, capsys):
    with pytest.raises(job.JobError, match="memory is true or false"):
        job.load(_write_job(tmp_path, BASIC + 'memory = "yes"\n', name="bad"))
    spec = job.load(_write_job(tmp_path, MEMORY.replace("REPORTS", str(tmp_path / "r"))))
    assert "memory   " in job.describe(spec, job.config(spec)) and "0 files" in job.describe(spec, job.config(spec))
    plain = job.load(_write_job(tmp_path, BASIC.replace("REPORTS", str(tmp_path / "r")), name="plain"))
    assert "  memory   " not in job.describe(plain, job.config(plain))


def test_only_the_last_runs_memories_are_kept_to_go_back_to(tmp_path):
    runs = tmp_path / "runs"
    for n in range(job.memory.KEEP + 3):
        (runs / f"20261110T0800{n:02d}Z-abcd").mkdir(parents=True)
        (runs / f"20261110T0800{n:02d}Z-abcd" / job.memory.SNAPSHOT).write_bytes(b"")
    job.memory.prune(runs)
    left = sorted(p.parent.name for p in runs.glob(f"*/{job.memory.SNAPSHOT}"))
    assert len(left) == job.memory.KEEP and left[0] == "20261110T080003Z-abcd"
