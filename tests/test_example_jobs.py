import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "examples/jobs/repo-digest/scripts/renovate_prs.py"
spec = importlib.util.spec_from_file_location("renovate_prs", SCRIPT)
renovate_prs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renovate_prs)


@pytest.mark.parametrize("title,labels,expected", [
    ("fix(container): update image ghcr.io/paperless-ngx/paperless-ngx (3.3.2 ➔ 3.3.3)", [], "patch"),
    ("feat(helm): update chart rook-ceph (v1.20.8 ➔ v1.21.0)", [], "minor"),
    ("feat(container)!: update image tdarr (2.91 ➔ 3.01)", [], "major"),
    ("chore(renovate-config)!: Update config presets", [], "major"),
    ("fix(container): update image itzg/minecraft-server (2a23f74 ➔ 0ce9423)", [], "digest"),
    ("Update dependency foo to v2", ["type/major"], "major"),
    ("Update dependency foo (patch)", [], "patch"),
    ("chore(deps): lock file maintenance", [], "lockfile"),
    ("Something else", [], "unknown"),
])
def test_kind(title, labels, expected):
    assert renovate_prs.kind(title, labels) == expected


def test_bucket_sorts_what_needs_no_judgement():
    assert renovate_prs.bucket("patch", "failing", 0) == "blocked: CI failing"
    assert renovate_prs.bucket("minor", "pending", 2) == "blocked: CI pending over a day"
    assert renovate_prs.bucket("major", "green", 0) == "check: major update"
    assert renovate_prs.bucket("minor", "none", 0) == "check: no CI result"
    assert renovate_prs.bucket("minor", "green", 30) == "review"  # age alone blocks nothing
