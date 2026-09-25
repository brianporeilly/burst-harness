from harness.guardrails import _is_git, _segments, _subcommand


def test_quoted_operators_do_not_split():
    segs = _segments('git commit -m "a && b" && git push --force origin main')
    assert len(segs) == 2
    assert _subcommand(_is_git(segs[1])) == ("push", ["--force", "origin", "main"])


def test_global_options_skipped():
    assert _subcommand(_is_git(["GIT_X=1", "git", "-C", "dir", "-c", "a=b", "switch", "-c", "x"]))[0] == "switch"
