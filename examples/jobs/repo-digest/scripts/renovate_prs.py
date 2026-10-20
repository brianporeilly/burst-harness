"""Open Renovate PRs across the token owner's repos, as JSON on stdout.

Standard library only. No token here: the gateway adds it on requests to
api.github.com.
"""

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
BODY_LIMIT = 3000


def get(path: str) -> dict:
    request = urllib.request.Request(API + path, headers={"Accept": "application/vnd.github+json",
                                                          "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def ci_state(repo: str, sha: str) -> str:
    runs = get(f"/repos/{repo}/commits/{sha}/check-runs?per_page=100").get("check_runs", [])
    if not runs:
        return "none"
    if any(run["status"] != "completed" for run in runs):
        return "pending"
    bad = {"failure", "timed_out", "cancelled", "action_required"}
    return "failing" if any(run["conclusion"] in bad for run in runs) else "green"


KINDS = ("major", "minor", "patch", "digest", "pin")
# `(1.2.3 ➔ 1.3.0)`, `(v1.2 -> v2.0)`, or `from 1.2 to 1.3`.
VERSIONS = re.compile(r"\(\s*([^\s()]+)\s*(?:➔|→|->)\s*([^\s()]+)\s*\)|from\s+(\S+)\s+to\s+(\S+)")
DIGEST = re.compile(r"[0-9a-f]{7,}")


def _parts(version: str) -> list[int]:
    return [int(part) for part in re.findall(r"\d+", version)[:3]]


def kind(title: str, labels: list[str]) -> str:
    """Renovate's update type: from a `type/...` label or a `(patch)` suffix if
    the repo's presets add them, else from the versions in the title, else from
    the conventional-commit prefix (`feat!:` major, `feat:` minor, `fix:` patch)."""
    lowered = title.lower()
    if "lock file maintenance" in lowered:
        return "lockfile"
    for name in KINDS:
        if f"type/{name}" in labels or lowered.endswith(f"({name})"):
            return name
    if match := VERSIONS.search(title):
        old, new = (match[1], match[2]) if match[1] else (match[3], match[4])
        if DIGEST.fullmatch(old) and DIGEST.fullmatch(new):
            return "digest"
        before, after = _parts(old), _parts(new)
        if before and after:
            for index, name in enumerate(("major", "minor", "patch")):
                if before[index:index + 1] != after[index:index + 1]:
                    return name
            return "patch"
    prefix = lowered.split(":", 1)[0] if ":" in lowered else ""
    if prefix.endswith("!"):
        return "major"
    if prefix.startswith("feat"):
        return "minor"
    if prefix.startswith("fix"):
        return "patch"
    return "unknown"


def bucket(kind: str, ci: str, age_days: int) -> str:
    """The sorting that needs no judgement; `review` is left to the model."""
    if ci == "failing":
        return "blocked: CI failing"
    if ci == "pending" and age_days >= 1:
        return "blocked: CI pending over a day"
    if kind in ("major", "unknown"):
        return "check: " + ("major update" if kind == "major" else "update type unknown")
    if ci != "green":
        return "check: no CI result"
    return "review"


def main() -> None:
    login = get("/user")["login"]
    query = urllib.parse.quote(f"is:pr is:open author:app/renovate user:{login}")
    found = get(f"/search/issues?q={query}&per_page=100")
    now = datetime.now(timezone.utc)
    prs = []
    for item in found.get("items", []):
        repo = item["repository_url"].removeprefix(f"{API}/repos/")
        pr = get(f"/repos/{repo}/pulls/{item['number']}")
        created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        body = item.get("body") or ""
        labels = [label["name"] for label in item.get("labels", [])]
        age, ci = (now - created).days, ci_state(repo, pr["head"]["sha"])
        update = kind(item["title"], labels)
        sorted_as = bucket(update, ci, age)
        prs.append({
            "repo": repo, "number": item["number"], "title": item["title"], "url": item["html_url"],
            "kind": update, "ci": ci, "age_days": age, "bucket": sorted_as, "labels": labels,
            "mergeable": pr.get("mergeable_state"),
            # Release notes only where a judgement is still needed.
            "body": "" if sorted_as.startswith("blocked") else
                    body[:BODY_LIMIT] + (" ...(shortened)" if len(body) > BODY_LIMIT else ""),
        })
    json.dump({"login": login, "count": len(prs), "prs": prs}, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
