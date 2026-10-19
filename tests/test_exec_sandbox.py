import asyncio

from harness.agent import SANDBOX_OFFLOAD_DIR, offload_storage
from harness.exec_sandbox import CommandSandbox
from harness.exec_target import DockerTarget, LocalTarget


def test_write_file_sends_content_on_stdin(tmp_path):
    # `env` stands in for `podman exec -i box`: it runs what follows, here.
    sandbox = CommandSandbox(["env"], stdin_prefix=["env"])
    content = b"x" * 300_000  # past Linux's 128 KiB limit on one argument
    target = tmp_path / "deep" / "file.bin"
    asyncio.run(sandbox.write_file(str(target), content))
    assert target.read_bytes() == content


def test_write_file_reports_failure(tmp_path):
    sandbox = CommandSandbox(["env"], stdin_prefix=["env"])
    (tmp_path / "taken").write_text("a file, not a directory")
    try:
        asyncio.run(sandbox.write_file(str(tmp_path / "taken" / "x"), b"data"))
    except OSError as err:
        assert "taken" in str(err)
    else:
        raise AssertionError("expected OSError")


def test_container_targets_pass_stdin_for_writes():
    sandbox = DockerTarget(binary="podman", container="box", repo="/workspace").sandbox()
    assert "-i" in sandbox.stdin_prefix and "-i" not in sandbox.prefix


def test_offloader_storage_is_in_the_target_unless_local(tmp_path):
    assert offload_storage(LocalTarget(repo=str(tmp_path)), "/state").base_dir == "/state"
    remote = DockerTarget(binary="podman", container="box", repo="/workspace")
    assert offload_storage(remote, "/state").base_dir == SANDBOX_OFFLOAD_DIR
