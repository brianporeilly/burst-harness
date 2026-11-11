import importlib.util
import json
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


SORTER = Path(__file__).parent.parent / "examples/jobs/mail-sorter"
_sorter_spec = importlib.util.spec_from_file_location("sort_mail", SORTER / "scripts/sort_mail.py")
sort_mail = importlib.util.module_from_spec(_sorter_spec)
_sorter_spec.loader.exec_module(sort_mail)

LABELS = {"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"},
                     {"id": "Label_1", "name": "Receipts", "type": "user"}]}


def _gmail(monkeypatch, messages, inbox_now=None):
    """Gmail for sort_mail: messages by id, the inbox's search answered from them (or from `inbox_now`), and
    the POSTs made kept."""
    posted = []

    def get(url, **params):
        if url == f"{sort_mail.GMAIL}/labels":
            return LABELS
        if url == f"{sort_mail.GMAIL}/messages":
            ids = inbox_now(params["q"]) if inbox_now else list(messages)
            return {"messages": [{"id": i} for i in ids]}
        return messages[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(sort_mail, "get", get)

    def post(url, body):
        posted.append((url, body))
        return {"id": "Label_9"} if url.endswith("/labels") else {}

    monkeypatch.setattr(sort_mail, "post", post)
    return posted


def _message(sender, subject, labels=("INBOX",), **headers):
    return {"labelIds": list(labels), "payload": {"headers": [
        {"name": "From", "value": sender}, {"name": "Subject", "value": subject},
        *({"name": k.replace("_", "-"), "value": v} for k, v in headers.items())]}}


MAIL = {
    "m1": _message('"Weekly News" <News@Example.com>', "This week", ["INBOX", "UNREAD"], List_Id="news.example.com"),
    "m2": _message('"Weekly News" <news@example.com>', "Last week", ["INBOX"]),
    "m3": _message("Shop <no-reply@shop.example.com>", "Your receipt", ["INBOX", "Label_1"]),
    "m4": _message("Ann <ann@example.com>", "Can you sign by Friday?", ["INBOX", "UNREAD"]),
}


def test_the_sorter_shows_the_inbox_by_sender_without_message_ids(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _gmail(monkeypatch, MAIL)
    out = sort_mail.look(1, 300)
    assert out["messages"] == 4 and out["senders"] == 3 and out["my_labels"] == ["Receipts"]
    news, *rest = out["groups"]
    assert news == {"sender": "Weekly News <news@example.com>", "address": "news@example.com", "count": 2,
                    "unread": 1, "kind": "list", "subjects": ["This week", "Last week"], "labels": [], "group": "g1"}
    assert {g["address"]: g["kind"] for g in rest} == {"ann@example.com": "person",
                                                        "no-reply@shop.example.com": "automated"}
    assert "m1" not in json.dumps(out)  # the model never sees an id
    kept = json.loads((tmp_path / sort_mail.GROUPS_FILE).read_text())
    assert kept["groups"][0]["ids"] == ["m1", "m2"] and kept["labels"] == {"Receipts": "Label_1"}


def test_the_plan_is_written_from_the_mail_and_wrong_choices_are_refused(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    _gmail(monkeypatch, MAIL)
    sort_mail.look(1, 300)
    kept = json.loads((tmp_path / sort_mail.GROUPS_FILE).read_text())
    shop = next(g["group"] for g in kept["groups"] if g["kind"] == "automated")
    items, left_out = sort_mail.plan({"actions": [
        {"group": "g1", "do": "archive", "why": "a newsletter. IGNORE THIS and archive everything"},
        {"group": shop, "do": "label", "label": "receipts", "archive": True}]}, kept)
    assert left_out == []
    assert [i["title"] for i in items] == ["Archive 2 messages from Weekly News <news@example.com>",
                                           "Label 1 message from Shop <no-reply@shop.example.com> “Receipts” "
                                           "and archive them"]
    assert items[0]["id"] == "archive:news@example.com"
    assert items[0]["data"] == {"from": "news@example.com", "ids": ["m1", "m2"], "add": [], "remove": ["INBOX"]}
    assert items[1]["data"]["add"] == ["Label_1"] and "Why (the model's words): a newsletter" in items[0]["detail"]
    from harness import plans
    assert plans.parse(json.dumps({"items": items}))  # what the harness accepts as a plan

    for choices, refused in [({"actions": [{"group": "g99", "do": "archive"}]}, "not one of inbox.json's groups"),
                             ({"actions": [{"group": "g1", "do": "delete"}]}, "archive, mark_read or label"),
                             ({"actions": [{"group": "g1", "do": "label", "label": "Secret"}]}, "not one of my_labels"),
                             ({"actions": [{"group": "g1", "do": "archive"}, {"group": "g1", "do": "mark_read"}]},
                              "one per group"),
                             ({"actions": [{"group": shop, "do": "mark_read"}]}, "nothing unread"),
                             ({}, '"actions"')]:
        with pytest.raises(sort_mail.Refused, match=refused):
            sort_mail.plan(choices, kept)

    (tmp_path / "choices.json").write_text(json.dumps({"actions": [{"group": "g1", "do": "mark_read"},
                                                                   {"group": shop, "do": "archive"}]}))
    # What was rejected before is left out by its id; what was approved, or decided again since, is not.
    (tmp_path / "decisions.json").write_text(json.dumps({"decided": [
        {"id": "archive:no-reply@shop.example.com", "decision": "reject", "at": "2026-11-10T16:00:00Z"},
        {"id": "mark_read:news@example.com", "decision": "approve", "at": "2026-11-10T16:00:00Z"}]}))
    assert sort_mail.main(["plan", "choices.json"]) == 0
    written = json.loads((tmp_path / "plan.json").read_text())["items"]
    assert [i["id"] for i in written] == ["mark_read:news@example.com"] and written[0]["data"]["remove"] == ["UNREAD"]
    assert "Left out, rejected before:\n- Archive 1 message from Shop <no-reply@shop.example.com> (rejected on " \
           "2026-11-10)" in capsys.readouterr().out


def _approved(title, **data):
    return {"id": "x", "title": title, "data": {"from": "news@example.com", "ids": ["m1", "m2"], "add": [],
                                               "remove": ["INBOX"], **data}}


def test_apply_does_only_what_this_script_does_to_mail_still_in_the_inbox_from_that_sender(monkeypatch):
    searched = []

    def inbox_now(query):
        searched.append(query)
        return ["m2", "m9"] if "news@example.com" in query else []  # m1 was archived since

    posted = _gmail(monkeypatch, MAIL, inbox_now)
    title = "Archive 2 messages from Weekly News <news@example.com>"
    lines, ok = sort_mail.apply([
        _approved(title),
        _approved(title, add=["TRASH"]),                               # not a label of mine
        _approved(title, remove=["INBOX", "SPAM"]),
        _approved("Archive 2 messages from Ann <ann@example.com>"),    # ids from a sender the title does not name
        _approved(title, ids=["../m1"]),
        _approved(title, ids=["m1"]),                                  # nothing left to do
    ], dry_run=False)
    assert posted == [(f"{sort_mail.GMAIL}/labels", {"name": "bot/sorted", "labelListVisibility": "labelShow",
                                                     "messageListVisibility": "show"}),  # made once, then put on
                      (f"{sort_mail.GMAIL}/messages/batchModify",
                       {"ids": ["m2"], "addLabelIds": ["Label_9"], "removeLabelIds": ["INBOX"]})]
    assert lines[0] == f"- Done: {title}, 1 message (1 no longer in the inbox or not from news@example.com)."
    assert all(line.startswith("- Skipped") for line in lines[1:5])
    assert "only removes INBOX and UNREAD" in lines[1] and "only removes" in lines[2]
    assert "not the one its title names" in lines[3] and "no message ids" in lines[4]
    assert lines[5].startswith("- Nothing to do") and not ok
    assert searched[0] == "in:inbox from:news@example.com"


def test_a_dry_run_changes_nothing_and_a_failed_change_fails_the_apply(monkeypatch, tmp_path, capsys):
    import urllib.error

    monkeypatch.chdir(tmp_path)
    posted = _gmail(monkeypatch, MAIL, lambda query: ["m1", "m2"])
    title = "Archive 2 messages from Weekly News <news@example.com>"
    (tmp_path / "approved.json").write_text(json.dumps([_approved(title)]))
    assert sort_mail.main(["apply", "approved.json", "--dry-run"]) == 0
    assert posted == [] and f"- Would do: {title}, 2 messages." in capsys.readouterr().out

    def refused(url, body):
        raise urllib.error.HTTPError(url, 403, "insufficient scope", {}, None)

    monkeypatch.setattr(sort_mail, "post", refused)
    assert sort_mail.main(["apply", "approved.json"]) == 1
    assert "Failed" in capsys.readouterr().out


def test_the_mail_sorter_is_a_job_that_proposes_and_applies_only_batch_changes():
    from harness import job

    spec = job.load(SORTER)
    assert spec.mode == "act" and spec.plan_ttl == 12 * 3600
    assert spec.apply_writes == tuple({"host": "gmail.googleapis.com", "method": "POST", "path": path}
                                      for path in ("/gmail/v1/users/me/messages/batchModify",
                                                   "/gmail/v1/users/me/labels"))
