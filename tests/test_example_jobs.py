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


WATCHER = Path(__file__).parent.parent / "examples/jobs/cluster-watcher/scripts/cluster_state.py"
_watcher_spec = importlib.util.spec_from_file_location("cluster_state", WATCHER)
cluster_state = importlib.util.module_from_spec(_watcher_spec)
_watcher_spec.loader.exec_module(cluster_state)


def _pod(name, phase, *, job="", restarts=0, finished=None, waiting=None, ready=True):
    status = {"restartCount": restarts, "ready": ready, "name": "c", "state": {}, "lastState": {}}
    if finished:
        status["lastState"] = {"terminated": {"finishedAt": finished}}
    if waiting:
        status["state"] = {"waiting": {"reason": waiting}}
    meta = {"namespace": "ns", "name": name, "creationTimestamp": "2026-10-01T00:00:00Z",
            "labels": {"job-name": job} if job else {}}
    return {"metadata": meta, "status": {"phase": phase, "containerStatuses": [status]}}


def test_pods_groups_failed_jobs_and_ignores_old_restarts(monkeypatch):
    recent = cluster_state.NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    items = [
        _pod("ok", "Running"),
        _pod("old-restarts", "Running", restarts=300, finished="2026-01-01T00:00:00Z"),
        _pod("flapping", "Running", restarts=4, finished=recent),
        _pod("crash", "Running", waiting="CrashLoopBackOff", ready=False),
        _pod("bootstrap-29869620-a", "Failed", job="bootstrap-29869620"),
        _pod("bootstrap-29869621-b", "Failed", job="bootstrap-29869621"),
        _pod("done", "Succeeded"),
    ]
    monkeypatch.setattr(cluster_state, "get", lambda api, path: {"items": items})
    found = cluster_state.pods("http://api")
    assert [p["pod"] for p in found["items"]] == ["ns/crash", "ns/flapping"]
    assert found["failed_jobs"] == [{"job": "ns/bootstrap", "failed_pods": 2, "latest": found["failed_jobs"][0]["latest"]}]


BRIEF = Path(__file__).parent.parent / "examples/jobs/daily-brief/scripts/brief_data.py"
_brief_spec = importlib.util.spec_from_file_location("brief_data", BRIEF)
brief_data = importlib.util.module_from_spec(_brief_spec)
_brief_spec.loader.exec_module(brief_data)


def _google(responses):
    """Stands in for brief_data.get: Google's answers by URL, with the queries asked kept."""
    asked = []

    def get(url, **params):
        asked.append((url, params))
        answer = responses[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return get, asked


def test_the_brief_s_calendar_is_today_in_order_with_clashes_and_answers_owed(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("America/Anchorage")
    events = {"items": [
        {"summary": "Dentist", "start": {"dateTime": "2026-11-07T15:00:00Z"}, "end": {"dateTime": "2026-11-07T16:00:00Z"}},
        {"summary": "Standup", "start": {"dateTime": "2026-11-07T09:30:00-09:00"},
         "end": {"dateTime": "2026-11-07T10:30:00-09:00"}, "hangoutLink": "https://meet.example.com/x",
         "attendees": [{"self": True, "responseStatus": "accepted"}, {"email": "a@example.com"}]},
        {"summary": "Planning", "start": {"dateTime": "2026-11-07T10:00:00-09:00"},
         "end": {"dateTime": "2026-11-07T11:00:00-09:00"},
         "attendees": [{"self": True, "responseStatus": "needsAction"}, {"email": "b@example.com"}]},
        {"summary": "Skipped sync", "start": {"dateTime": "2026-11-07T10:15:00-09:00"},
         "end": {"dateTime": "2026-11-07T10:45:00-09:00"},
         "attendees": [{"self": True, "responseStatus": "declined"}]},
        {"summary": "Cancelled", "status": "cancelled", "start": {"date": "2026-11-07"}, "end": {"date": "2026-11-08"}},
        {"summary": "Birthday", "start": {"date": "2026-11-07"}, "end": {"date": "2026-11-08"}},
    ]}
    get, asked = _google({f"{brief_data.CALENDAR}/calendars/primary/events": events})
    monkeypatch.setattr(brief_data, "get", get)
    out = brief_data.calendar(zone, ["primary"], datetime(2026, 11, 7, 6, 30, tzinfo=zone))
    assert out["date"] == "Saturday 07 November 2026"
    assert [e["title"] for e in out["events"]] == ["Birthday", "Dentist", "Standup", "Planning", "Skipped sync"]
    by = {e["title"]: e for e in out["events"]}
    assert by["Birthday"]["start"] == "all day" and by["Dentist"]["start"] == "06:00"  # 15:00Z, in Anchorage
    assert by["Standup"]["video"] and by["Standup"]["attendees"] == 1
    assert by["Planning"]["my_response"] == "needsAction" and by["Planning"]["clashes_with"] == ["Standup"]
    assert "clashes_with" not in by["Skipped sync"]  # declined: no clash
    # The day asked for is Anchorage's.
    assert asked[0][1]["timeMin"] == "2026-11-07T00:00:00-09:00" and asked[0][1]["singleEvents"] == "true"


def test_the_brief_s_mail_lists_people_and_counts_lists_and_notifications(monkeypatch):
    import time

    now = int(time.time() * 1000)

    def message(sender, subject, labels=(), ago_hours=1, **headers):
        return {"labelIds": list(labels), "snippet": f"About {subject}", "internalDate": str(now - ago_hours * 3600_000),
                "payload": {"headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject},
                                        *({"name": k.replace("_", "-"), "value": v} for k, v in headers.items())]}}

    messages = {
        "1": message("Ann <ann@example.com>", "Can you sign the form by Friday?", ["UNREAD"]),
        "2": message("Bob <bob@example.com>", "Lunch?", ["UNREAD", "IMPORTANT"]),
        "3": message('"Weekly News" <news@example.com>', "This week", List_Unsubscribe="<mailto:x>"),
        "4": message("Shop <no-reply@shop.example.com>", "Your order shipped"),
        "5": message("Old <old@example.com>", "Last week", ago_hours=30),
        "6": message('"Weekly News" <news@example.com>', "Again", List_Id="news.example.com"),
    }
    responses = {f"{brief_data.GMAIL}/messages": {"messages": [{"id": i} for i in messages]}}
    responses |= {f"{brief_data.GMAIL}/messages/{i}": m for i, m in messages.items()}
    get, asked = _google(responses)
    monkeypatch.setattr(brief_data, "get", get)
    out = brief_data.mail(24)
    assert [m["subject"] for m in out["from_people"]] == ["Lunch?", "Can you sign the form by Friday?"]
    assert out["from_people"][0]["important"] and out["from_people"][1]["unread"]
    assert out["lists_and_notifications"] == {"Weekly News": 2, "Shop": 1}
    assert "-category:promotions" in asked[0][1]["q"] and asked[1][1]["format"] == "metadata"


def test_a_part_of_the_brief_that_cannot_be_read_says_so(monkeypatch):
    import io
    import urllib.error

    refused = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b'{"error": "insufficient scope"}'))
    get, _ = _google({f"{brief_data.GMAIL}/messages": refused})
    monkeypatch.setattr(brief_data, "get", get)
    assert brief_data.mail(24)["unavailable"].startswith("HTTP 403: {\"error\": \"insufficient scope\"}")
