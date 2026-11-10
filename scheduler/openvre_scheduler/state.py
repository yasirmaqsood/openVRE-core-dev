"""What state a session is in, from its Deployment and pods.

Used both by GET /interactive-sessions/<name> and by the audit watcher, so
the API and the audit record always agree on why a session failed.
"""

# Container waiting reasons that mean the session will not start without a fix.
BLOCKING_REASONS = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CrashLoopBackOff",
                    "CreateContainerConfigError", "CreateContainerError", "RunContainerError"}


def _true_conditions(obj):
    return {c.get("type"): c for c in (obj.get("status") or {}).get("conditions") or [] if c.get("status") == "True"}


def container_states(pods):
    """A compact list of every container's state, for the audit record."""
    out = []
    for pod in pods:
        st = pod.get("status") or {}
        for kind, key in (("init", "initContainerStatuses"), ("app", "containerStatuses")):
            for cs in st.get(key) or []:
                state = cs.get("state") or {}
                name, detail = next(iter(state.items()), ("unknown", {}))
                last = (cs.get("lastState") or {}).get("terminated") or {}
                out.append({
                    "pod": pod.get("metadata", {}).get("name", ""),
                    "container": cs.get("name"),
                    "kind": kind,
                    "image": cs.get("image"),
                    "ready": cs.get("ready", False),
                    "restart_count": cs.get("restartCount", 0),
                    "state": name,
                    "reason": (detail or {}).get("reason", ""),
                    "message": (detail or {}).get("message", ""),
                    "exit_code": (detail or {}).get("exitCode"),
                    "last_terminated_reason": last.get("reason", ""),
                    "last_exit_code": last.get("exitCode"),
                })
    return out


def evaluate(dep, pods):
    """Return {"state": Running|Pending|Failed, "reason", "message"}."""
    if ((dep.get("status") or {}).get("readyReplicas") or 0) >= 1:
        return {"state": "Running", "reason": "", "message": ""}

    # The pod could not even be created (quota, admission policy, Pod Security, ...).
    cond = _true_conditions(dep)
    if "ReplicaFailure" in cond:
        c = cond["ReplicaFailure"]
        return {"state": "Failed", "reason": c.get("reason", "ReplicaFailure"), "message": c.get("message", "")}
    for c in (dep.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Progressing" and c.get("status") == "False":
            return {"state": "Failed", "reason": c.get("reason", "ProgressDeadlineExceeded"), "message": c.get("message", "")}

    pending_reason = ""
    for pod in pods:
        st = pod.get("status") or {}
        if st.get("phase") == "Failed":
            return {"state": "Failed", "reason": st.get("reason", "PodFailed"), "message": st.get("message", "")}
        for cs in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
            waiting = (cs.get("state") or {}).get("waiting") or {}
            reason = waiting.get("reason", "")
            if reason in BLOCKING_REASONS:
                return {"state": "Failed", "reason": reason, "message": waiting.get("message", "")}
            pending_reason = reason or pending_reason
        sched = next((c for c in st.get("conditions") or [] if c.get("type") == "PodScheduled"), None)
        if sched and sched.get("status") == "False" and not pending_reason:
            pending_reason = sched.get("reason", "Unschedulable")
    return {"state": "Pending", "reason": pending_reason, "message": ""}
