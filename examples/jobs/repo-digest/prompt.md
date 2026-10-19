Write a digest of the open Renovate pull requests across my GitHub repositories.

1. Run `python3 /workspace/scripts/renovate_prs.py > /workspace/prs.json`. It lists them as JSON: repository, title, URL, age, CI state, labels, and the PR body (Renovate puts release notes there), shortened.
2. Group the PRs by repository, and within a repository by kind (patch, minor, major, lock file maintenance).
3. For each, decide: **safe to merge** (patch or minor, CI green, nothing alarming in the notes), **check first** (a major, or notes that mention breaking changes, removals, or migrations: say which, in one line), or **blocked** (CI failing, or pending for over a day).
4. Put the PRs that are safe to merge first, as a checklist with links, then the rest.

If the script fails, report the error. Do not try to reach GitHub some other way.
