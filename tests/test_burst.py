"""orchestrator.burst: a burst's output to its file and stdout, its status, and its signals passed on once."""

import os
import signal
import subprocess
import sys
import time

# Counts the SIGINTs it gets until told to stop (a file appears), or until a second one, then says how many.
CHILD = r'''
import os, signal, sys, time
got = []
signal.signal(signal.SIGINT, lambda *_: got.append(1))
print("ready", flush=True)
end = time.monotonic() + 10
while time.monotonic() < end and not os.path.exists(sys.argv[1]) and len(got) < 2:
    time.sleep(0.02)
time.sleep(0.2)  # a SIGINT passed on late would still be counted
print(f"interrupted {len(got)} time(s)", flush=True)
sys.exit(130 if got else 0)
'''


def _burst(tmp_path, *command, **kwargs):
    return subprocess.Popen([sys.executable, "-m", "orchestrator.burst", str(tmp_path / "output"), "--", *command],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, **kwargs)


def _ready(burst):
    assert burst.stdout.readline() == b"ready\n"


def test_the_output_goes_to_the_file_and_stdout_and_the_status_is_the_harness_s(tmp_path):
    (tmp_path / "output").write_text("=== burst 1 ===\n")
    burst = _burst(tmp_path, sys.executable, "-c",
                   "import sys; print('working', flush=True); print('oops', file=sys.stderr); sys.exit(3)")
    out, err = burst.communicate(timeout=30)
    assert burst.returncode == 3 and out == b"working\noops\n" and not err
    assert (tmp_path / "output").read_text() == "=== burst 1 ===\nworking\noops\n"  # appended


def test_a_sigterm_reaches_the_harness_as_one_sigint(tmp_path):
    burst = _burst(tmp_path, sys.executable, "-c", CHILD, str(tmp_path / "stop"))
    _ready(burst)
    burst.send_signal(signal.SIGTERM)  # as Kubernetes does: to the pod's first process alone
    out, _ = burst.communicate(timeout=30)
    assert burst.returncode == 130 and b"interrupted 1 time(s)" in out


def test_a_sigint_for_the_group_reaches_the_harness_once_not_twice(tmp_path):
    burst = _burst(tmp_path, sys.executable, "-c", CHILD, str(tmp_path / "stop"))
    _ready(burst)
    os.killpg(burst.pid, signal.SIGINT)  # as Stop does, and Ctrl-C
    time.sleep(0.3)
    (tmp_path / "stop").touch()
    out, _ = burst.communicate(timeout=30)
    assert burst.returncode == 130 and b"interrupted 1 time(s)" in out


def test_a_harness_that_cannot_start_is_told_in_the_file(tmp_path):
    burst = _burst(tmp_path, str(tmp_path / "no-such-program"))
    burst.communicate(timeout=30)
    assert burst.returncode == 127 and "could not start the burst" in (tmp_path / "output").read_text()
