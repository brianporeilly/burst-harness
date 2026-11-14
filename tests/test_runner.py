"""Runners (harness/runner.py): hosts of your own, as a plain host over ssh or as a remote container engine.

A fake `ssh` on PATH stands in for the host: it runs the one command line it is given with `sh -c` on this
machine, the way sshd hands it to the account's shell, and logs every argv.
"""

import json
import os
import socket
import stat
import sys
import textwrap

import pytest

from harness import runner as runner_module
from harness.config import HarnessConfig
from harness.exec_target import DockerTarget
from harness.runner import RunnerError, SshTarget, parse

FAKE_SSH = textwrap.dedent('''\
    #!{python}
    """ssh, as far as runner.py uses it: options, the destination, one command line. Every call is logged."""
    import json, os, socket, subprocess, sys, time
    args = sys.argv[1:]
    with open(os.environ["FAKE_SSH_LOG"], "a") as log:
        log.write(json.dumps(args) + "\\n")
    with_value, flags, i = ("-o", "-i", "-p", "-L", "-O"), set(), 0
    forward = control = None
    while i < len(args) and args[i].startswith("-"):
        if args[i] in with_value:
            if args[i] == "-L":
                forward = args[i + 1]
            if args[i] == "-O":
                control = args[i + 1]
            i += 2
        else:
            flags.add(args[i])
            i += 1
    destination, command = args[i], args[i + 1:]
    if os.environ.get("FAKE_SSH_REFUSE"):
        sys.exit("Permission denied (publickey).")
    if control:
        sys.exit(0)
    if "-N" in flags:
        local = forward.split(":", 1)[0]
        server = socket.socket(socket.AF_UNIX)
        server.bind(local)
        server.listen()
        time.sleep(60)
        sys.exit(0)
    stdin = subprocess.DEVNULL if "-n" in flags else None
    env = dict(os.environ, HOME=os.environ["FAKE_SSH_HOME"])
    sys.exit(subprocess.run(["sh", "-c", " ".join(command)], stdin=stdin, env=env).returncode)
''')


@pytest.fixture
def ssh(tmp_path, monkeypatch):
    """The fake ssh first on PATH; (its log, the host's home)."""
    bin_dir, home, log = tmp_path / "bin", tmp_path / "home", tmp_path / "ssh.log"
    bin_dir.mkdir()
    home.mkdir()
    fake = bin_dir / "ssh"
    fake.write_text(FAKE_SSH.format(python=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("FAKE_SSH_HOME", str(home))
    runner_module.close_all()
    yield log, home
    runner_module.close_all()  # before the next test's fake ssh replaces this one


def _calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _cfg(**settings):
    return HarnessConfig.load(None, {"runners": ("vm1=ssh://agent@vm1.example.com:2222",), **settings})


# --- the runners setting ---


def test_runner_entries_name_a_host_and_what_runs_there():
    assert parse("vm1=ssh://agent@vm1.example.com") == runner_module.Runner(
        "vm1", "ssh", host="vm1.example.com", user="agent", path="burst")
    assert parse("vm1=ssh://vm1:2222/srv/work/").path == "/srv/work"
    assert parse("vm1=ssh://vm1/~/work").path == "work"  # in the home
    box = parse("box=podman+ssh://agent@box/run/user/1000/podman/podman.sock")
    assert (box.kind, box.path, box.destination) == ("podman", "/run/user/1000/podman/podman.sock", "agent@box")
    assert parse("big=docker+ssh://big").path == "/var/run/docker.sock"
    here = parse("here=docker+unix:///run/docker.sock")
    assert (here.kind, here.over_ssh, here.path) == ("docker", False, "/run/docker.sock")
    assert parse("my-vm=ssh://h").key_var == "MY_VM_SSH_KEY"
    assert parse("my-vm=ssh://h").known_hosts_var == "MY_VM_SSH_KNOWN_HOSTS"


@pytest.mark.parametrize("entry, says", [
    ("ssh://host", "NAME=URL"),
    ("VM=ssh://host", "NAME=URL"),
    ("vm=https://host", "is not ssh://"),
    ("vm=ssh://", r"is not ssh://\[user@\]host"),
    ("vm=ssh://user:pw@host", r"is not ssh://\[user@\]host"),
    ("vm=ssh://host:port", "not a number"),
    ("box=podman+ssh://host", "socket's path"),
    ("here=podman+unix://host/sock", "socket on this machine"),
])
def test_a_runner_entry_that_cannot_be_used_is_refused(entry, says):
    with pytest.raises(RunnerError, match=says):
        parse(entry)


def test_a_runner_decides_the_target_and_must_be_listed():
    runners = ("vm1=ssh://vm1", "box=podman+ssh://box/run/podman.sock", "big=docker+ssh://big")
    assert HarnessConfig.load(None, {"runners": runners, "runner": "vm1"}).target == "ssh"
    assert HarnessConfig.load(None, {"runners": runners, "runner": "box", "target": "local"}).target == "podman"
    assert HarnessConfig.load(None, {"runners": runners, "runner": "big"}).target == "docker"
    assert HarnessConfig.load(None, {"runners": runners}).target == "local"
    with pytest.raises(RunnerError, match="No runner named 'nope'.*vm1, box, big"):
        HarnessConfig.load(None, {"runners": runners, "runner": "nope"})
    with pytest.raises(RunnerError, match="listed twice"):
        HarnessConfig.load(None, {"runners": ("a=ssh://x", "a=ssh://y")})
    with pytest.raises(RunnerError, match="--runner NAME"):
        HarnessConfig.load(None, {"target": "ssh"})


def test_runners_come_from_the_environment_too(monkeypatch):
    monkeypatch.setenv("HARNESS_RUNNERS", "vm1=ssh://vm1,box=podman+ssh://box/run/podman.sock")
    monkeypatch.setenv("HARNESS_RUNNER", "box")
    cfg = HarnessConfig.load(None)
    assert cfg.target == "podman" and [r.name for r in runner_module.runners(cfg)] == ["vm1", "box"]


def test_a_repo_cannot_choose_a_runner():
    with pytest.raises(ValueError, match="may not set"):
        HarnessConfig.load('runner = "vm1"\n')


# --- ssh itself ---


def test_ssh_checks_the_host_and_uses_the_key_it_is_given(ssh, monkeypatch):
    monkeypatch.setenv("VM1_SSH_KEY", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----")
    monkeypatch.setenv("VM1_SSH_KNOWN_HOSTS", "[vm1.example.com]:2222 ssh-ed25519 AAAA")
    link = runner_module.connection(runner_module.find(_cfg(), "vm1"))
    argv = list(link.argv)
    assert argv[0] == "ssh" and argv[-1] == "agent@vm1.example.com"
    assert ["-p", "2222"] == argv[argv.index("-p"):argv.index("-p") + 2]
    for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes", "ControlMaster=auto"):
        assert option in argv
    key = argv[argv.index("-i") + 1]
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600 and open(key).read().endswith("KEY-----\n")
    known = next(a for a in argv if a.startswith("UserKnownHostsFile="))
    assert open(known.split("=", 1)[1]).read() == "[vm1.example.com]:2222 ssh-ed25519 AAAA\n"
    assert "-----BEGIN" not in " ".join(argv)  # by file, never in argv
    link.close()
    assert not os.path.exists(link.dir)


def test_without_a_key_your_own_ssh_setup_applies(ssh):
    argv = runner_module.connection(runner_module.find(_cfg(), "vm1")).argv
    assert "-i" not in argv and not any(a.startswith("UserKnownHostsFile=") for a in argv)
    assert "StrictHostKeyChecking=yes" in argv  # an unknown host is refused, never trusted on first sight


def test_an_argv_reaches_the_host_as_it_was_given():
    from harness.ssh_exec import command

    ssh = ["ssh", "-o", "BatchMode=yes", "agent@vm1"]
    assert command([*ssh, "--", "sh", "-c", "cd '/x y' && echo \"$HOME\""]) == [
        "ssh", "-n", "-o", "BatchMode=yes", "agent@vm1", "sh -c 'cd '\"'\"'/x y'\"'\"' && echo \"$HOME\"'"]
    assert command(["-i", *ssh, "--", "cat"])[:2] == ["ssh", "-o"]  # stdin passed through: no -n
    assert command(["-t", "-i", *ssh, "--", "bash"])[1] == "-tt"
    with pytest.raises(SystemExit):
        command(ssh)


# --- a plain host ---


def test_a_run_on_a_plain_host_works_in_a_directory_of_its_own(ssh, capsys):
    log, home = ssh
    target = runner_module.acquire(_cfg(runner="vm1"), slug="Fix the README")
    assert target.owned and target.workdir.startswith(f"{home}/burst/Fix-the-README-")
    assert target.repo == f"{target.workdir}/repo" and target.scratch == f"{target.workdir}/.burst"
    assert stat.S_IMODE(os.stat(target.workdir).st_mode) == 0o700
    assert "Working on vm1 (agent@vm1.example.com)" in capsys.readouterr().out

    # Commands, quoting, stdin and files, all on the host.
    target.run_shell(f"mkdir -p {target.repo}", cwd="/")
    assert target.run(["printf", "%s|", "a b", "it's", "$HOME"]).stdout == "a b|it's|$HOME|"
    assert target.run_shell("cat", stdin="through stdin").stdout == "through stdin"
    target.write_file("notes/a.txt", "one\ntwo\n")
    assert target.read_file("notes/a.txt") == "one\ntwo\n"
    assert open(f"{target.repo}/notes/a.txt").read() == "one\ntwo\n"
    assert target.attach_hint() == f"--runner vm1 --runner-workdir {target.workdir}"
    assert "SSH host vm1" in target.describe()

    # One connection for the run: every call shares the control socket.
    assert all(any(a.startswith("ControlPath=") for a in call) for call in _calls(log))
    assert target.release() == f"Removed {target.workdir} on vm1."
    assert not os.path.exists(target.workdir)


def test_the_agents_tools_run_on_the_host(ssh, tmp_path):
    import asyncio

    target = runner_module.acquire(_cfg(runner="vm1"), slug="t")
    target.run_shell(f"mkdir -p {target.repo}", cwd="/")
    sandbox = target.sandbox()
    result = asyncio.run(sandbox.execute("echo \"$((1 + 2))\" > three.txt && cat three.txt"))
    assert result.exit_code == 0 and result.stdout.strip() == "3"
    assert open(f"{target.repo}/three.txt").read() == "3\n"
    target.release()


def test_a_kept_directory_is_attached_to_again_or_replaced_once_gone(ssh, capsys):
    first = runner_module.acquire(_cfg(runner="vm1"), slug="t")
    again = runner_module.acquire(_cfg(runner="vm1", runner_workdir=first.workdir), slug="t")
    assert (again.workdir, again.owned) == (first.workdir, False)
    assert again.release() == ""  # not this run's to remove
    assert os.path.isdir(first.workdir)
    first.release()
    capsys.readouterr()
    fresh = runner_module.acquire(_cfg(runner="vm1", runner_workdir=first.workdir), slug="t")
    assert fresh.owned and fresh.workdir != first.workdir
    assert f"{first.workdir} is gone from vm1" in capsys.readouterr().out
    fresh.release()


def test_a_plain_host_refuses_a_gateways_settings(ssh):
    for settings in ({"egress": "allowlist"}, {"egress": "log"}, {"egress_inject": ("git",)}):
        with pytest.raises(RunnerError, match="no gateway"):
            runner_module.acquire(_cfg(runner="vm1", **settings), slug="t")


def test_a_host_that_cannot_be_reached_or_lacks_git_says_so(ssh, monkeypatch, tmp_path):
    from harness.exec_target import ExecError

    monkeypatch.setenv("FAKE_SSH_REFUSE", "1")
    with pytest.raises(ExecError, match="Permission denied"):
        runner_module.acquire(_cfg(runner="vm1"), slug="t")
    monkeypatch.delenv("FAKE_SSH_REFUSE")
    monkeypatch.setattr(runner_module, "REQUIRED_COMMANDS", ("sh", "no-such-command-here"))
    with pytest.raises(RunnerError, match="missing commands the harness needs: no-such-command-here"):
        runner_module.acquire(_cfg(runner="vm1"), slug="t")
    assert os.listdir(ssh[1] / "burst") == []  # the directory it made is removed again


def test_a_task_runs_end_to_end_on_a_plain_host(ssh, tmp_path, origin, capsys):
    """The harness's own run, from clone to push, with every command on the host."""
    from harness import cli

    from . import fake_llama

    finish = {"tool": "finish_with_pr", "args": {"title": "Fix it", "body": "Fixed."}}
    server, _ = fake_llama.serve([
        {"tool": "sandbox_shell", "args": {"command": "echo fixed > FIXED.md && pwd"}}, finish, {"text": "Done."}])
    try:
        args = ["--provider", "llamacpp", "--base-url", f"http://127.0.0.1:{server.server_port}",
                "--runners", "vm1=ssh://agent@vm1.example.com", "--runner", "vm1", "--repo-url", str(origin)]
        assert cli.main([*args, "start", "t1", "--task", "Fix it."]) == 0
        out = capsys.readouterr().out
        assert "Working on vm1" in out and f"{ssh[1]}/burst/t1-" in out
        assert "Created harness/t1 from origin/main" in out
        # Pushed (no PR: the origin is a path), so the result names the directory but it was not kept.
        assert json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])["state"] == "pushed_no_pr"
        assert "Removed" in out and "on vm1." in out  # pushed: the directory is not kept
    finally:
        server.shutdown()
    shown = os.popen(f"git --git-dir={origin} show harness/t1:FIXED.md").read()
    assert shown == "fixed\n"


def test_release_removes_a_kept_directory(ssh, capsys):
    from harness import cli

    kept = runner_module.acquire(_cfg(runner="vm1"), slug="t")
    capsys.readouterr()
    assert cli.main(["--runners", "vm1=ssh://agent@vm1.example.com", "--runner", "vm1", "--runner-workdir",
                     kept.workdir, "release"]) == 0
    assert f"Removed {kept.workdir} on vm1." in capsys.readouterr().out
    with pytest.raises(SystemExit, match="plain-host runner"):
        cli.main(["--runner-workdir", kept.workdir, "release"])


def test_sessions_do_not_run_on_a_runner(ssh):
    from harness import cli

    with pytest.raises(SystemExit, match="Sessions don't run on a runner"):
        cli.main(["--runners", "box=podman+ssh://box/run/podman.sock", "--runner", "box", "session", "list"])


# --- a container engine ---


def test_a_runners_engine_is_reached_through_its_forwarded_socket(ssh, monkeypatch, capsys):
    log, _ = ssh
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    cfg = HarnessConfig.load(None, {"runners": ("box=podman+ssh://agent@box/run/user/1000/podman/podman.sock",),
                                    "runner": "box"})
    assert runner_module.connect_engine(cfg) == "CONTAINER_HOST"
    local = os.environ["CONTAINER_HOST"].removeprefix("unix://")
    assert stat.S_ISSOCK(os.stat(local).st_mode)
    forward = next(call for call in _calls(log) if "-N" in call)
    assert forward[forward.index("-L") + 1] == f"{local}:/run/user/1000/podman/podman.sock"
    assert forward[-1] == "agent@box" and "ExitOnForwardFailure=yes" in forward
    assert "podman on runner box (agent@box)" in capsys.readouterr().out
    assert runner_module.connect_engine(cfg) == "CONTAINER_HOST"  # asked again: the same tunnel
    assert sum("-N" in call for call in _calls(log)) == 1
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(local)  # something listens: the forward


def test_an_engine_that_cannot_be_reached_says_so(ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_REFUSE", "1")
    cfg = HarnessConfig.load(None, {"runners": ("big=docker+ssh://big",), "runner": "big"})
    with pytest.raises(RunnerError, match="Could not reach docker on runner big.*Permission denied"):
        runner_module.connect_engine(cfg)


def test_a_local_socket_is_used_as_it_is(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    cfg = HarnessConfig.load(None, {"runners": ("here=docker+unix:///run/docker.sock",), "runner": "here"})
    assert runner_module.connect_engine(cfg) == "DOCKER_HOST" and os.environ["DOCKER_HOST"] == "unix:///run/docker.sock"
    assert runner_module.connect_engine(HarnessConfig.load(None, {"runners": ("vm1=ssh://vm1",)})) == ""


def test_a_container_on_a_runner_is_reattached_through_it():
    on = DockerTarget(repo="/w", container="burst-t-abc", binary="podman", runner="box")
    assert on.attach_hint() == "--runner box --target podman --docker-container burst-t-abc"
    assert DockerTarget(repo="/w", container="c", binary="docker").attach_hint() == "--target docker --docker-container c"


def test_the_target_keeps_its_files_in_the_runs_directory():
    assert DockerTarget(repo="/w", container="c").scratch == "/tmp/burst-harness"
    assert SshTarget(repo="/h/burst/t/repo", workdir="/h/burst/t").scratch == "/h/burst/t/.burst"
