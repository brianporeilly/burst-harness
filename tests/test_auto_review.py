"""The review loop (TaskRunner auto_review): each PR a burst opens is reviewed; the reviewer asking for changes
continues the task by itself, the review in its next burst; approving leaves the merge to a person; and the loop
stops for a person when fixing doesn't settle it."""

import pytest

pytest.importorskip("fastapi")

from harness import cli, git, review, terminal
from orchestrator.slack import SlackBot
from orchestrator.tasks import TasksError

from .test_tasks import _runner, _settled, harness  # noqa: F401 (harness: a fixture)

PR = "https://github.com/me/proj/pull/7"
LOOP = {"model": "claude-opus-5-5", "max_cycles": 2}


def _task(tmp_path, harness, loop=LOOP):  # noqa: F811
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "Add f", "slug": "t", "engine": "strands", "provider": "llamacpp",
                   "base_url": "http://llm.lan:8080", "auto_review": loop})
    return runner


def _burst(runner, harness, index, **result):  # noqa: F811
    """End the index'th burst, and wait for what the loop does next to start (or for the task to settle)."""
    harness.started[index][1].finish(0, **result)
    for _ in range(200):
        if len(harness.started) > index + 1 or (runner.task("t")["state"] not in ("running", "queued")
                                                and "t" not in runner.running):
            return
        __import__("threading").Event().wait(0.02)


def _is_review(harness, index):  # noqa: F811
    return "review" in harness.argv(index)


def test_a_pr_is_reviewed_fixed_and_reviewed_again_until_approved(tmp_path, harness):  # noqa: F811
    runner = _task(tmp_path, harness)
    _burst(runner, harness, 0, state="pr", pr=PR, said={"summary": "added f"})
    assert _is_review(harness, 1)
    argv = harness.argv(1)
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5" and argv[-4:-2] == ["--pr", PR]

    _burst(runner, harness, 1, state="reviewed", said={"review": "1. **blocking** f returns 1.", "by": "Claude Code",
                                                       "verdict": "request_changes", "posted": PR + "#r1"})
    assert len(harness.started) == 3 and not _is_review(harness, 2)  # the task carries on, by itself
    assert runner.task("t")["review_status"]["cycles"] == 1

    _burst(runner, harness, 2, state="pr", pr=PR, said={"summary": "f returns 2"})
    assert _is_review(harness, 3)
    _burst(runner, harness, 3, state="reviewed", said={"review": "Fixed.", "by": "Claude Code", "verdict": "approve",
                                                       "posted": PR + "#r2"})
    done = _settled(runner, "t", "pr")
    assert len(harness.started) == 4  # approved: nothing more starts, and nothing is merged
    assert done["review_status"] == {"cycles": 1, "verdict": "approve", "stopped": None, "approved_as_comment": False}
    assert done["said"] == {"summary": "f returns 2"}  # the task's own, as the author's last burst left it


def test_the_loop_stops_after_max_cycles_of_fixes(tmp_path, harness):  # noqa: F811
    runner = _task(tmp_path, harness, {"model": "claude-opus-5-5", "max_cycles": 1})
    _burst(runner, harness, 0, state="pr", pr=PR)
    _burst(runner, harness, 1, state="reviewed", said={"review": "A bug.", "verdict": "request_changes", "posted": "x"})
    _burst(runner, harness, 2, state="pr", pr=PR)
    _burst(runner, harness, 3, state="reviewed", said={"review": "Another.", "verdict": "request_changes", "posted": "x"})
    done = _settled(runner, "t", "pr")
    assert len(harness.started) == 4
    assert done["review_status"]["stopped"] == "the reviewer still asks for changes after 1 rounds of fixes"

    runner.resume("t")  # a person continuing starts the count again
    assert runner.task("t")["review_status"]["cycles"] == 0 and runner.task("t")["review_status"]["stopped"] is None


def test_a_blocking_point_raised_again_stops_the_loop(tmp_path, harness):  # noqa: F811
    runner = _task(tmp_path, harness, {"model": "", "max_cycles": 5})
    _burst(runner, harness, 0, state="pr", pr=PR)
    _burst(runner, harness, 1, state="reviewed", said={"review": "1. **blocking** M0.5 has no test.",
                                                       "verdict": "request_changes", "posted": "x"})
    _burst(runner, harness, 2, state="pr", pr=PR)
    _burst(runner, harness, 3, state="reviewed", said={"review": "1. M0.5: **still blocking**, the answer doesn't hold.",
                                                       "verdict": "request_changes", "posted": "x"})
    done = _settled(runner, "t", "pr")
    assert len(harness.started) == 4
    assert "the author and the reviewer disagree" in done["review_status"]["stopped"]


@pytest.mark.parametrize(("result", "why"), [
    ({"state": "reviewed", "said": {"review": "Some notes.", "verdict": "comment"}}, "the review gave no verdict"),
    ({"state": "failed"}, "the review failed"),
])
def test_a_review_without_a_verdict_stops_the_loop(tmp_path, harness, result, why):  # noqa: F811
    runner = _task(tmp_path, harness)
    _burst(runner, harness, 0, state="pr", pr=PR)
    _burst(runner, harness, 1, **result)
    done = _settled(runner, "t", "pr")
    assert len(harness.started) == 2 and done["review_status"]["stopped"] == why


def test_an_approval_posted_as_a_comment_is_said(tmp_path, harness):  # noqa: F811
    runner = _task(tmp_path, harness)
    _burst(runner, harness, 0, state="pr", pr=PR)
    _burst(runner, harness, 1, state="reviewed", said={"review": "Fine.", "verdict": "approve", "comment": PR + "#c"})
    record = _settled(runner, "t", "pr")
    assert record["review_status"]["approved_as_comment"] is True
    line = SlackBot._about(None, "reviewed", {**record, "bursts": [{"state": "reviewed",
                                                                    "said": {"verdict": "approve", "comment": "c"}}]})
    assert "counts for nothing in the branch rules" in line


def test_a_burst_without_a_pr_is_not_reviewed_and_no_loop_no_review(tmp_path, harness):  # noqa: F811
    runner = _task(tmp_path, harness)
    _burst(runner, harness, 0, state="paused", said={"summary": "s", "next_step": "n"})
    assert len(harness.started) == 1 and _settled(runner, "t", "paused")

    plain = _runner(tmp_path / "b")
    plain.create({"repo": "me/proj", "prompt": "Add f", "slug": "t", "engine": "strands", "provider": "llamacpp",
                  "base_url": "http://llm.lan:8080"})
    harness.started[1][1].finish(0, state="pr", pr=PR)
    _settled(plain, "t", "pr")
    assert len(harness.started) == 2 and plain.task("t")["auto_review"] is None


def test_turning_the_loop_on_reviews_the_open_pr_now_and_off_stops_it(tmp_path, harness):  # noqa: F811
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "Add f", "slug": "t", "engine": "strands", "provider": "llamacpp",
                   "base_url": "http://llm.lan:8080"})
    harness.started[0][1].finish(0, state="pr", pr=PR)
    _settled(runner, "t", "pr")
    runner.auto_review("t", {"on": True, "model": "claude-opus-5-5"})
    assert _is_review(harness, 1) and runner.task("t")["auto_review"] == {"model": "claude-opus-5-5", "max_cycles": 3}
    assert runner._read("t")["review_by"] == "auto-review"
    harness.started[1][1].finish(0, state="reviewed", said={"verdict": "request_changes", "review": "x", "posted": "y"})
    runner.auto_review("t", {"on": False})  # off before the next step is looked at: a race either way, but off stays off
    assert runner.task("t")["auto_review"] is None and runner.task("t")["review_status"] is None


def test_a_loop_is_checked_when_asked_for():
    from dataclasses import replace

    from harness.config import HarnessConfig
    from orchestrator.tasks import TaskRunner

    runner = TaskRunner(replace(HarnessConfig.load(None, {}), state_dir="/nonexistent"))
    for bad, why in (({"model": "claude-opus-5-5", "max_cycles": 0}, "1 to 10"), ("yes", "auto_review is"),
                     ({"model": "a b"}, "model's id"), ({"model": "m", "profile": "p"}, "not both")):
        with pytest.raises(TasksError, match=why):
            runner._auto_review(bad)
    assert runner._auto_review(None) is None and runner._auto_review({"model": ""}) == {"model": "", "max_cycles": 3}


def test_a_bursts_own_answer_to_the_review_is_not_given_back_to_it(monkeypatch):
    class Ctx:
        repo_slug, branch, cfg, answering_review = "github.com/me/proj", "harness/t", None, False

        class target:  # noqa: N801
            @staticmethod
            def run_shell(command, check=True):
                return None

    monkeypatch.setattr(git, "find_pr", lambda repo, head, cfg=None: PR)
    monkeypatch.setattr(git, "run", lambda *args, **kwargs: "2026-11-25T10:00:00+00:00")
    said = [{"who": "coding-bot", "when": "w", "where": "", "text": review.ANSWER_MARK + "\nFixed f."}]
    monkeypatch.setattr(git, "feedback", lambda pr, cfg=None, since="": list(said))
    ctx = Ctx()
    assert cli._review_feedback(ctx) == "" and ctx.answering_review is False  # only its own answer: nothing new
    said.append({"who": "review-bot", "when": "w", "where": "review (request_changes)", "text": "Still a bug."})
    prompt = cli._review_feedback(ctx)
    assert "Still a bug." in prompt and "Fixed f." not in prompt and ctx.answering_review is True
    assert "posted on it as your answer to the review" in prompt


def test_the_answer_is_posted_on_the_pr(monkeypatch):
    posted = []
    monkeypatch.setattr(git, "comment_pr", lambda url, body, cfg=None: posted.append((url, body)) or url + "#c9")

    class Ctx:
        cfg = None

    assert terminal._answer(Ctx(), PR, "Changed f; kept the name, as the plan says.") == \
        f"Answered the review on the PR: {PR}#c9"
    [(url, body)] = posted
    assert url == PR and body.startswith(review.ANSWER_MARK) and body.endswith("as the plan says.")

    def refused(url, body, cfg=None):
        raise git.GitError("forgejo refused the comment (403)")

    monkeypatch.setattr(git, "comment_pr", refused)
    assert terminal._answer(Ctx(), PR, "x").startswith("Could not post the answer to the review: forgejo refused")
