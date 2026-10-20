Write a digest of the open Renovate pull requests across my GitHub repositories.

1. Run `python3 /workspace/scripts/renovate_prs.py > /workspace/prs.json`. If it fails, report the error; do not try to reach GitHub some other way.
2. Read the file with `jq` or `python3` rather than viewing it whole. Each PR has `repo`, `number`, `title`, `url`, `kind` (major, minor, patch, ...), `ci` (green, failing, pending, none), `age_days`, `bucket`, and `body` (Renovate's release notes, shortened; empty for blocked PRs).
3. The script has already sorted each PR into its `bucket`. Keep those. Only the PRs in `review` need you: read their notes and move a PR to **check first** if they mention breaking changes, removals, or migrations (say which, in one line). The rest of `review` is **safe to merge**.
4. Report three sections, each grouped by repository:
   - **Safe to merge**: a checklist with links.
   - **Check first**: the PR, a link, and one line on why.
   - **Blocked**: the PR, a link, and the reason from its bucket. One line each.

Put the report straight into finish_report. Do not write it out as a message first: nobody reads your messages, and it doubles the time a local model takes.
