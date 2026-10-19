"""Open Renovate PRs across the token owner's repos, as JSON on stdout.

Standard library only. No token here: the gateway adds it on requests to
api.github.com.
"""

import json
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
        prs.append({
            "repo": repo, "number": item["number"], "title": item["title"], "url": item["html_url"],
            "age_days": (now - created).days, "labels": [label["name"] for label in item.get("labels", [])],
            "ci": ci_state(repo, pr["head"]["sha"]), "mergeable": pr.get("mergeable_state"),
            "body": body[:BODY_LIMIT] + (" ...(shortened)" if len(body) > BODY_LIMIT else ""),
        })
    json.dump({"login": login, "count": len(prs), "prs": prs}, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
