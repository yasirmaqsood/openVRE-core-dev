"""Admin-only audit files for interactive sessions, under OPENVRE_INTERACTIVE_AUDIT_BASE.

Audit writes are best effort: a failure is logged and never breaks session
create or delete.
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from . import state
from .k8s import K8sError, q

log = logging.getLogger("openvre_scheduler.audit")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _path_segment(value, max_len, fallback):
    # Same folder naming as the previous scheduler, so audit trees stay consistent.
    value = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").lower()).strip("-")
    return value[:max_len] or fallback


def session_dir(settings, tool_id, tool_name, user_id, k8s_name):
    """<audit base>/<tool>/<user>/<k8s name>. Always computed here, never taken from a request."""
    tool_seg = _path_segment(tool_id or tool_name, 48, "tool")
    user_seg = _path_segment(user_id, 64, "user")
    path = os.path.join(settings.audit_base, tool_seg, user_seg, k8s_name)
    real = os.path.realpath(path)
    base = os.path.realpath(settings.audit_base)
    if not (real == base or real.startswith(base + os.sep)):
        raise ValueError("audit path escapes the audit base")
    return path


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o750)
    except OSError:
        pass


def write_json(path, payload):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, indent=2, ensure_ascii=True) + "\n")
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass


def _read_json(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - audit must never break the API
        log.warning("audit write skipped", extra={"error": str(e)})
        return None


def write_text(path, text):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass


_meta_lock = threading.Lock()


def update_meta(audit_dir, **fields):
    """Merge fields into session-meta.json. The watcher thread and a delete may both write."""
    meta_path = os.path.join(audit_dir, "session-meta.json")
    with _meta_lock:
        meta = _read_json(meta_path)
        meta.update(fields)
        write_json(meta_path, meta)
        return meta


def write_create(audit_dir, spec, objects):
    update_meta(
        audit_dir,
        execution_id=spec.name,
        session_id=spec.session_id,
        user_id=spec.user_id,
        tool_id=spec.tool_id,
        tool_name=spec.tool_name,
        template=spec.template,
        template_version=spec.template_version,
        project=spec.project,
        execution=spec.execution,
        working_dir=spec.working_dir,
        namespace=spec.namespace,
        image=spec.image,
        access_url=spec.path_prefix + "/",
        path_prefix=spec.path_prefix,
        audit_host_path=audit_dir,
        created_at=spec.created_at,
        expires_at=spec.expires_at,
        # starting -> running | failed; at delete: stopped (a failure stays "failed").
        status="starting",
        last_phase="create",
    )
    for obj in objects:
        write_json(os.path.join(audit_dir, f"{obj['kind'].lower()}.json"), obj)


# Snapshots ------------------------------------------------------------------

def collect(kube, ns, name, tail_lines=200):
    """Live state of a session: Deployment, pods, events, container states, last log lines."""
    pods = kube.list(f"/api/v1/namespaces/{q(ns)}/pods", f"app={name}")
    dep = kube.get(f"/apis/apps/v1/namespaces/{q(ns)}/deployments/{q(name)}")
    # Every object named after the session: Deployment, ReplicaSets (quota or policy
    # rejections show up there, when no pod exists), pods, Service, Ingress, ConfigMap.
    events = [e for e in kube.list(f"/api/v1/namespaces/{q(ns)}/events")
              if str((e.get("involvedObject") or {}).get("name", "")).startswith(name)]
    events.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
    logs = {}
    for pod in pods:
        pod_name = pod.get("metadata", {}).get("name", "")
        statuses = {cs.get("name"): cs for cs in ((pod.get("status") or {}).get("containerStatuses") or [])
                    + ((pod.get("status") or {}).get("initContainerStatuses") or [])}
        spec = pod.get("spec") or {}
        for c in (spec.get("initContainers") or []) + (spec.get("containers") or []):
            cname = c.get("name")
            runs = [False] + ([True] if (statuses.get(cname) or {}).get("restartCount") else [])
            for previous in runs:
                query = {"container": cname, "tailLines": str(tail_lines), "limitBytes": str(256 * 1024), "timestamps": "true"}
                if previous:
                    query["previous"] = "true"
                try:
                    text = kube.get_text(f"/api/v1/namespaces/{q(ns)}/pods/{q(pod_name)}/log", query)
                except K8sError as e:
                    text = f"# could not read log: {e.message}\n"
                if text:
                    logs[f"{pod_name}__{cname}{'__previous' if previous else ''}"] = text
    return {"deployment": dep, "pods": pods, "events": events, "containers": state.container_states(pods), "logs": logs}


def write_snapshot(audit_dir, phase, snap):
    write_json(os.path.join(audit_dir, f"k8s-events-{phase}.json"), {"items": snap["events"]})
    write_json(os.path.join(audit_dir, f"containers-{phase}.json"), snap["containers"])
    if snap["deployment"]:
        write_json(os.path.join(audit_dir, f"deployment-live-{phase}.json"), snap["deployment"])
    if snap["pods"]:
        write_json(os.path.join(audit_dir, f"pod-{phase}.json"), snap["pods"][0] if len(snap["pods"]) == 1 else snap["pods"])
    for key, text in snap["logs"].items():
        write_text(os.path.join(audit_dir, "logs", phase, f"{key}.log"), text)


def watch_session(kube, settings, audit_dir, ns, name, timeout=None, interval=3):
    """Follow a new session until it is ready, fails, or times out, then record
    a snapshot and the outcome in session-meta.json. Returns the final status."""
    timeout = settings.audit_watch_sec if timeout is None else timeout
    deadline = time.monotonic() + timeout
    while True:
        try:
            dep = kube.get(f"/apis/apps/v1/namespaces/{q(ns)}/deployments/{q(name)}")
            if dep is None:
                return "gone"  # deleted before it was ready; the delete snapshot covers it
            pods = kube.list(f"/api/v1/namespaces/{q(ns)}/pods", f"app={name}")
            st = state.evaluate(dep, pods)
            now = utc_now()
            if st["state"] == "Running":
                write_snapshot(audit_dir, "running", collect(kube, ns, name, settings.audit_log_tail_lines))
                update_meta(audit_dir, status="running", ready_at=now, last_phase="running", last_snapshot_at=now)
                return "running"
            if st["state"] == "Failed" or time.monotonic() > deadline:
                timed_out = st["state"] != "Failed"
                phase = "timeout" if timed_out else "failed"
                reason = "NotReadyInTime" if timed_out else st["reason"]
                message = (f"not ready after {timeout}s; last reason: {st['reason'] or 'none'}"
                           if timed_out else st["message"])
                write_snapshot(audit_dir, phase, collect(kube, ns, name, settings.audit_log_tail_lines))
                update_meta(audit_dir, status="failed", reason=reason, message=message,
                            failed_at=now, last_phase=phase, last_snapshot_at=now)
                log.warning("session failed", extra={"session": name, "namespace": ns, "reason": reason,
                                                     "error": message[:300]})
                return "failed"
        except K8sError as e:
            log.warning("audit watch: Kubernetes API error, retrying", extra={"session": name, "error": e.message})
        time.sleep(interval)


def snapshot_delete(kube, settings, audit_dir, ns, name, reason="delete"):
    """Record the final state and logs before a session is deleted."""
    try:
        write_snapshot(audit_dir, "delete", collect(kube, ns, name, settings.audit_log_tail_lines))
    except K8sError as e:
        log.warning("audit: delete snapshot incomplete", extra={"session": name, "error": e.message})
    meta = _read_json(os.path.join(audit_dir, "session-meta.json"))
    now = utc_now()
    status = "failed" if meta.get("status") == "failed" else "stopped"
    update_meta(audit_dir, status=status, last_phase="delete", deleted_at=now,
                last_snapshot_at=now, delete_reason=reason)
