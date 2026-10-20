"""What needs attention in a Kubernetes cluster, as compact JSON on stdout.

    python3 cluster_state.py https://10.0.0.5:6443

Standard library only, and read-only: every request is a GET. No token here:
the gateway adds it on requests to the API server. Each section is boiled
down to what is wrong, so the model reads a short list rather than the
cluster; a section the token cannot read says so instead of failing the run.
"""

import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)
EVENT_WINDOW = timedelta(hours=24)
CERT_WARN = timedelta(days=21)
BACKUP_STALE = timedelta(hours=36)
RESTARTS = 3  # restarts at or above this, the last one in EVENT_WINDOW, make a pod worth mentioning
LIMIT = 40    # entries per section; the rest are counted, not listed


def get(api: str, path: str) -> dict:
    request = urllib.request.Request(api.rstrip("/") + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def when(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def age(text: str | None) -> str:
    moment = when(text)
    if moment is None:
        return "never"
    hours = (NOW - moment).total_seconds() / 3600
    return f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.0f}d ago"


def condition(obj: dict, kind: str = "Ready") -> dict:
    return next((c for c in (obj.get("status") or {}).get("conditions") or [] if c.get("type") == kind), {})


def name(obj: dict) -> str:
    meta = obj.get("metadata") or {}
    return f"{meta['namespace']}/{meta['name']}" if meta.get("namespace") else meta.get("name", "?")


def capped(items: list) -> dict:
    return {"count": len(items), "items": items[:LIMIT]}


def nodes(api: str) -> dict:
    found = []
    for node in get(api, "/api/v1/nodes")["items"]:
        problems = [f"{c['type']}={c['status']}" for c in node["status"].get("conditions", [])
                    if (c["type"] == "Ready") != (c["status"] == "True")]
        if node["spec"].get("unschedulable"):
            problems.append("cordoned")
        found.append({"node": name(node), "problems": problems} if problems else None)
    unhealthy = [n for n in found if n]
    return {"total": len(found), "unhealthy": unhealthy}


def job_of(pod: dict) -> str:
    """The Job (or CronJob, without its run's suffix) a pod belongs to, or ""."""
    job = ((pod.get("metadata") or {}).get("labels") or {}).get("job-name", "")
    return f"{pod['metadata']['namespace']}/{re.sub(r'-[0-9]{6,}$', '', job)}" if job else ""


def pods(api: str) -> dict:
    bad, failed_jobs = [], {}
    for pod in get(api, "/api/v1/pods")["items"]:
        status = pod.get("status") or {}
        phase = status.get("phase")
        if phase == "Succeeded":
            continue
        if phase == "Failed" and (job := job_of(pod)):
            # A failing job leaves a pod per attempt: one line per job, not per pod.
            entry = failed_jobs.setdefault(job, {"job": job, "failed_pods": 0, "latest": ""})
            entry["failed_pods"] += 1
            entry["latest"] = max(entry["latest"], (pod.get("metadata") or {}).get("creationTimestamp") or "")
            continue
        reasons, restarts, recent = [], 0, ""
        for c in status.get("containerStatuses") or []:
            restarts += c.get("restartCount", 0)
            finished = ((c.get("lastState") or {}).get("terminated") or {}).get("finishedAt")
            if (moment := when(finished)) and NOW - moment < EVENT_WINDOW:
                recent = max(recent, finished)
            waiting = (c.get("state") or {}).get("waiting") or {}
            if waiting.get("reason") and waiting["reason"] != "ContainerCreating":
                reasons.append(f"{c['name']}: {waiting['reason']}")
            elif not c.get("ready") and phase == "Running" and not (c.get("state") or {}).get("terminated"):
                reasons.append(f"{c['name']}: not ready")
        if phase not in ("Running", "Succeeded"):
            reasons.insert(0, phase or "Unknown")
        # A long-lived pod's restart count only ever grows: it matters if the last one was recent.
        if reasons or (restarts >= RESTARTS and recent):
            entry = {"pod": name(pod), "phase": phase, "restarts": restarts, "why": reasons[:3]}
            if recent:
                entry["last_restart"] = age(recent)
            bad.append(entry)
    bad.sort(key=lambda p: (-len(p["why"]), -p["restarts"]))
    jobs = sorted(failed_jobs.values(), key=lambda j: j["latest"], reverse=True)
    for job in jobs:
        job["latest"] = age(job["latest"])
    return {**capped(bad), "failed_jobs": jobs[:LIMIT]}


def events(api: str) -> dict:
    grouped: dict[tuple, dict] = {}
    for event in get(api, "/api/v1/events?fieldSelector=type%3DWarning")["items"]:
        last = when(event.get("lastTimestamp") or event.get("eventTime") or (event.get("metadata") or {}).get("creationTimestamp"))
        if last is None or NOW - last > EVENT_WINDOW:
            continue
        involved = event.get("involvedObject") or {}
        key = (involved.get("namespace"), involved.get("kind"), involved.get("name"), event.get("reason"))
        entry = grouped.setdefault(key, {"object": f"{involved.get('kind')} {involved.get('namespace') or ''}/{involved.get('name')}",
                                         "reason": event.get("reason"), "count": 0,
                                         "message": (event.get("message") or "")[:200]})
        entry["count"] += event.get("count") or 1
    return capped(sorted(grouped.values(), key=lambda e: -e["count"]))


def certificates(api: str) -> dict:
    found = []
    for cert in get(api, "/apis/cert-manager.io/v1/certificates")["items"]:
        ready, expires = condition(cert), when((cert.get("status") or {}).get("notAfter"))
        if ready.get("status") != "True":
            found.append({"certificate": name(cert), "problem": f"not ready: {ready.get('message', 'no Ready condition')}"[:200]})
        elif expires and expires - NOW < CERT_WARN:
            found.append({"certificate": name(cert), "problem": f"expires {expires:%Y-%m-%d}"})
    return capped(found)


def flux(api: str) -> dict:
    found = []
    for kind, path in (("Kustomization", "/apis/kustomize.toolkit.fluxcd.io/v1/kustomizations"),
                       ("HelmRelease", "/apis/helm.toolkit.fluxcd.io/v2/helmreleases")):
        for obj in get(api, path)["items"]:
            if (obj.get("spec") or {}).get("suspend"):
                found.append({"object": f"{kind} {name(obj)}", "problem": "suspended"})
                continue
            ready = condition(obj)
            if ready.get("status") != "True":
                found.append({"object": f"{kind} {name(obj)}", "problem": (ready.get("message") or "not ready")[:200]})
    return capped(found)


def backups(api: str) -> dict:
    found = []
    for policy in get(api, "/apis/kopiur.home-operations.com/v1alpha1/snapshotpolicies")["items"]:
        last = (policy.get("status") or {}).get("lastSuccessfulSnapshot")
        moment, ready = when(last), condition(policy)
        if ready.get("status") != "True":
            found.append({"backup": f"kopiur {name(policy)}", "problem": (ready.get("message") or "not ready")[:200]})
        elif moment is None or NOW - moment > BACKUP_STALE:
            found.append({"backup": f"kopiur {name(policy)}", "problem": f"last snapshot {age(last)}"})
    for cluster in get(api, "/apis/postgresql.cnpg.io/v1/clusters")["items"]:
        for kind in ("Ready", "ContinuousArchiving", "LastBackupSucceeded"):
            c = condition(cluster, kind)
            if c and c.get("status") != "True":
                found.append({"backup": f"cnpg {name(cluster)}", "problem": f"{kind}: {c.get('message') or c.get('reason')}"[:200]})
    return capped(found)


def volumes(api: str) -> dict:
    pending = [{"pvc": name(p), "phase": p["status"].get("phase")}
               for p in get(api, "/api/v1/persistentvolumeclaims")["items"] if p["status"].get("phase") != "Bound"]
    return capped(pending)


SECTIONS = {"nodes": nodes, "pods": pods, "warning_events_24h": events, "certificates": certificates,
            "flux": flux, "backups": backups, "volumes": volumes}


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: cluster_state.py https://API-SERVER:6443")
    api = sys.argv[1]
    out: dict = {"checked_at": NOW.strftime("%Y-%m-%dT%H:%MZ")}
    for section, collect in SECTIONS.items():
        try:
            out[section] = collect(api)
        except urllib.error.HTTPError as err:
            # 403: the token may not read this; 404: the cluster does not have it.
            out[section] = {"unavailable": f"HTTP {err.code}" + (" (not permitted)" if err.code == 403 else
                                                                  " (not installed)" if err.code == 404 else "")}
        except (urllib.error.URLError, OSError, ValueError, KeyError) as err:
            out[section] = {"unavailable": f"{type(err).__name__}: {err}"[:200]}
    json.dump(out, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
