"""The sandbox's memory limit and CPUs, read in it and told to the agent (harness/resources.py)."""

import pytest

from harness import job, resources
from harness.exec_target import CommandResult, LocalTarget

GI = 2**30


@pytest.mark.parametrize("probe, expected", [
    (f"{8 * GI}|600000 100000|||{64 * GI}|16", ("8Gi", "6 CPUs")),  # cgroup v2, a pod's limits
    (f"{8 * GI}|max 100000|||{64 * GI}|16", ("8Gi", "16 CPUs")),  # no CPU quota: the CPUs it sees
    (f"{1536 * 2**20}||150000|100000|{64 * GI}|4", ("1.5Gi", "1.5 CPUs")),  # cgroup v1
    (f"{512 * 2**20}|100000 100000|||{64 * GI}|", ("512Mi", "1 CPU")),
    (f"max|max 100000|||{64 * GI}|16", ("", "")),  # no limit of its own
    (f"9223372036854771712||-1|100000|{64 * GI}|16", ("", "")),  # v1's unlimited
    ("", ("", "")),
])
def test_the_limits_are_read_from_the_sandboxs_cgroup(probe, expected):
    assert resources.limits(probe) == expected


class Sandbox:
    """A target whose shell answers the probe, counting how often it is asked."""

    def __init__(self, answer, where="pod x"):
        self.answer, self.where, self.asked = answer, where, 0
        self.repo = "/workspace"

    def describe(self):
        return self.where

    def run_shell(self, command, **kwargs):
        self.asked += 1
        return CommandResult(0, self.answer, "")


def test_the_agent_is_told_once_per_sandbox_and_not_on_this_machine(monkeypatch, capsys):
    monkeypatch.setattr(resources, "_seen", {})
    sandbox = Sandbox(f"{8 * GI}|600000 100000|||{64 * GI}|16")
    line = resources.note(sandbox)
    assert line.startswith("Resources: this sandbox may use at most 8Gi of memory and 6 CPUs.")
    assert "CARGO_BUILD_JOBS" in line and resources.note(sandbox) == line and sandbox.asked == 1
    assert "told the sandbox's limits: 8Gi of memory, 6 CPUs." in capsys.readouterr().out
    assert resources.note(Sandbox(f"max||||{64 * GI}|16", where="host y")) == ""
    assert resources.note(LocalTarget(repo="/tmp")) == ""

    class Broken(Sandbox):
        def run_shell(self, command, **kwargs):
            raise OSError("gone")

    assert resources.note(Broken("", where="pod z")) == ""  # the agent goes without, the run goes on


def test_a_jobs_rules_and_the_strands_prompt_carry_it(tmp_path, monkeypatch):
    from harness import agent

    monkeypatch.setattr(resources, "_seen", {})
    sandbox = Sandbox(f"{4 * GI}|200000 100000|||{64 * GI}|16")
    (tmp_path / "digest").mkdir()
    (tmp_path / "digest" / "job.toml").write_text("")
    (tmp_path / "digest" / "prompt.md").write_text("x")
    rules = job._rules(sandbox, job.load(tmp_path / "digest"), has_scripts=False, file=True)
    assert rules.rstrip().endswith("run one heavy build at a time.") and "at most 4Gi of memory and 2 CPUs" in rules
    assert agent._limits(sandbox).startswith("\n\nResources: this sandbox may use at most 4Gi")
