import json

import pytest

from harness import containers, egress
from harness.config import HarnessConfig
from harness.exec_target import refused_summary

GATEWAY = {"binary": "podman", "name": "burst-t-abc-egress", "status": "running"}


@pytest.fixture
def gateway(monkeypatch):
    """One running gateway with a log of events and an approvals file, both in memory."""
    box = {"events": [], "approvals": []}
    monkeypatch.setattr(egress, "gateways", lambda: [GATEWAY])
    monkeypatch.setattr(egress, "events", lambda g: box["events"])
    monkeypatch.setattr(egress, "read_approvals", lambda g: list(box["approvals"]))
    monkeypatch.setattr(egress, "write_approvals", lambda g, a: box.__setitem__("approvals", a))
    return box


def deny(ident, host, port=443, kind="connect", ts="2026-10-07T10:00:00Z"):
    event = {"ts": ts, "sandbox": "burst-t-abc", "kind": kind, "verdict": "deny", "id": ident, "host": host}
    return {**event, "port": port} if port else event


def test_destination_drops_the_default_port():
    assert egress.destination(deny("a-1", "example.com")) == "example.com"
    assert egress.destination(deny("a-1", "example.com", None, "dns")) == "example.com"
    assert egress.destination(deny("a-1", "llm.lan", 8080)) == "llm.lan:8080"


def test_pending_groups_by_destination_and_hides_the_approved(gateway):
    gateway["events"] = [
        deny("a-1", "example.com", ts="2026-10-07T10:00:00Z"), deny("a-2", "example.com", None, "dns", ts="2026-10-07T10:00:05Z"),
        deny("a-3", "other.com"), {"ts": "x", "kind": "connect", "verdict": "allow", "id": "a-4", "host": "ok.com", "port": 443},
        deny("a-5", "github.com", kind="request"), deny("a-6", "-", kind="tls"),
    ]
    gateway["approvals"] = [{"allow": "other.com", "id": "a-3"}]
    (row,) = egress.pending()
    assert (row["id"], row["destination"], row["times"], row["sandbox"]) == ("a-2", "example.com", 2, "burst-t-abc")


def test_approve_writes_an_entry_per_scope(gateway, tmp_path, monkeypatch):
    cfg = HarnessConfig(state_dir=str(tmp_path))
    gateway["events"] = [deny("a-1", "example.com"), deny("a-2", "llm.lan", 8080)]
    assert "example.com" in egress.approve(cfg, "a-1")
    assert gateway["approvals"] == [{"allow": "example.com", "id": "a-1"}]
    egress.approve(cfg, "a-1", "once")  # approving again replaces, it does not pile up
    assert gateway["approvals"] == [{"allow": "example.com", "id": "a-1", "once": True}]
    monkeypatch.setattr(egress.time, "time", lambda: 1000)
    egress.approve(cfg, "a-2", "2h")
    assert gateway["approvals"][-1] == {"allow": "llm.lan:8080", "id": "a-2", "expires": 8200}
    assert containers.approved(cfg) == []


def test_permanent_approval_joins_every_later_allowlist(gateway, tmp_path):
    cfg = HarnessConfig(state_dir=str(tmp_path), egress_allow=("github.com",))
    gateway["events"] = [deny("a-1", "example.com")]
    assert "from now on" in egress.approve(cfg, "a-1", "permanent")
    assert containers.approved(cfg) == ["example.com"]
    assert containers.allowlist(cfg) == ["github.com", "example.com"]
    assert "Revoked example.com from the permanent list and 1 running" in egress.revoke(cfg, "example.com")
    assert containers.approved(cfg) == [] and gateway["approvals"] == []


def test_approve_refuses_what_it_cannot_find_or_should_not_allow(gateway, tmp_path):
    cfg = HarnessConfig(state_dir=str(tmp_path))
    gateway["events"] = [deny("a-1", "github.com", kind="request")]
    with pytest.raises(egress.EgressError, match="No running gateway"):
        egress.approve(cfg, "zz-9")
    with pytest.raises(egress.EgressError, match="was not refused for where"):
        egress.approve(cfg, "a-1")
    with pytest.raises(egress.EgressError, match="Not a scope"):
        egress.approve(cfg, "a-1", "forever")
    with pytest.raises(egress.EgressError, match="Not a scope"):
        egress.approve(cfg, "a-1", "0")


def test_allow_and_revoke_check_the_host(tmp_path, monkeypatch):
    cfg = HarnessConfig(state_dir=str(tmp_path))
    monkeypatch.setattr(egress, "gateways", lambda: [])
    egress.allow(cfg, "*.Example.com")
    egress.allow(cfg, "llm.lan:8080")
    assert "already" in egress.allow(cfg, "llm.lan:8080")
    assert containers.approved(cfg) == ["*.example.com", "llm.lan:8080"]
    for bad in ("", "http://example.com", "a b", "example.com/path", "host:port"):
        with pytest.raises(egress.EgressError):
            egress.allow(cfg, bad)
    assert "was not approved anywhere" in egress.revoke(cfg, "nope.com")


def test_refused_summary_names_each_destination_once():
    lines = [deny("a-1", "example.com"), deny("a-2", "example.com", None, "dns"), deny("a-3", "llm.lan", 8080),
             deny("a-4", "github.com", kind="request")]
    text = refused_summary("\n".join(json.dumps(e) for e in lines).encode() + b"\nnot json\n")
    assert "Refused: example.com, llm.lan:8080." in text
    assert refused_summary(b"") == ""
