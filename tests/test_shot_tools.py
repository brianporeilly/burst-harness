"""The sandbox image's screenshot tools (images/sandbox/web-shot, window-shot): their steps and their arguments.
Taking a screenshot needs Chromium, or Xvfb, xdotool and ImageMagick: the image's build checks that (Containerfile),
and this runs it only where they are."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SANDBOX = Path(__file__).resolve().parent.parent / "images" / "sandbox"
sys.path.insert(0, str(SANDBOX))
import shot_steps  # noqa: E402


def _run(tool, *args, cwd=None):
    return subprocess.run([sys.executable, str(SANDBOX / tool), *args], capture_output=True, text=True, check=False,
                          cwd=cwd, timeout=60)


def test_steps_are_parsed_and_a_wrong_one_says_what_steps_there_are():
    assert shot_steps.parse("tap:360,900") == ("tap", "360", "900")
    assert shot_steps.parse("key:Space") == ("key", "Space")
    assert shot_steps.parse("type:hello, world") == ("type", "hello, world")
    assert shot_steps.parse("wait:1.5") == ("wait", "1.5")
    assert shot_steps.parse("shot:after tap.png") == ("shot", "after tap.png")
    for wrong in ("tap:1", "click:1,2", "wait:soon", "shot:"):
        with pytest.raises(ValueError, match="is not a step: tap:X,Y, key:NAME"):
            shot_steps.parse(wrong)


def test_the_tools_say_how_to_use_them_and_refuse_wrong_arguments():
    for tool in ("web-shot", "window-shot"):
        helped = _run(tool, "--help")
        assert helped.returncode == 0 and "tap:X,Y" in helped.stdout and "software" in helped.stdout
    wrong = _run("web-shot", "dist", "--step", "click:1,2")
    assert wrong.returncode == 2 and "'click:1,2' is not a step" in wrong.stderr
    assert "is not WxH" in _run("window-shot", "--size", "big", "--", "true").stderr
    assert "say what to run, after --." in _run("window-shot", "--wait", "1").stderr


@pytest.mark.skipif(not all(shutil.which(c) for c in ("Xvfb", "xdotool", "magick")), reason="no Xvfb, xdotool, magick")
def test_window_shot_saves_the_display_and_says_when_the_program_exited_first(tmp_path):
    taken = _run("window-shot", "--wait", "1", "--step", "key:Space", "--out", "a.png", "--", "sleep", "30", cwd=tmp_path)
    assert taken.returncode == 0 and (tmp_path / "a.png").stat().st_size > 0, taken.stderr
    gone = _run("window-shot", "--wait", "1", "--", "sh", "-c", "exit 3", cwd=tmp_path)
    assert gone.returncode == 1 and "The program exited (status 3)" in gone.stderr
