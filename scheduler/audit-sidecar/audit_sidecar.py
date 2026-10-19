"""Workspace file-change auditor. This file is the sidecar image command."""
import json
import os
import time
from datetime import datetime, timezone

watch_path = os.environ.get("WATCH_PATH", "").strip() or "/workspace"
audit_dir = "/audit-root"
session_id = os.environ.get("SESSION_ID", "").strip() or "unknown-session"
user_id = os.environ.get("USER_ID", "").strip() or "unknown-user"
working_dir = os.environ.get("WORKING_DIR", "").strip()
execution_id = os.environ.get("EXECUTION_ID", "").strip()
interval = int(os.environ.get("INTERVAL_SEC", "5"))
exclude_rel_prefixes = [
    p.strip().strip("/")
    for p in os.environ.get("EXCLUDE_REL_PREFIXES", "").split(",")
    if p.strip()
]

os.makedirs(audit_dir, mode=0o750, exist_ok=True)
try:
    os.chmod(audit_dir, 0o750)
except Exception:
    pass
event_file = os.path.join(audit_dir, "events.jsonl")
state_file = os.path.join(audit_dir, "latest-state.json")


def now():
    return datetime.now(timezone.utc).isoformat()


def is_excluded(rel):
    rel = rel.replace("\\", "/").lstrip("./")
    for prefix in exclude_rel_prefixes:
        if rel == prefix or rel.startswith(prefix + "/"):
            return True
    return False


def walk_state(base):
    state = {}
    if not os.path.isdir(base):
        return state
    for root, dirs, files in os.walk(base):
        rel_root = os.path.relpath(root, base)
        if rel_root == ".":
            rel_root = ""
        keep = []
        for d in dirs:
            rel = d if not rel_root else rel_root + "/" + d
            if not is_excluded(rel):
                keep.append(d)
        dirs[:] = keep
        for fname in files:
            rel = fname if not rel_root else rel_root + "/" + fname
            if is_excluded(rel):
                continue
            p = os.path.join(root, fname)
            try:
                st = os.stat(p)
            except FileNotFoundError:
                continue
            state[rel] = {"size": st.st_size, "mtime": int(st.st_mtime)}
    return state


def write_event(payload):
    payload["ts"] = now()
    payload["session_id"] = session_id
    payload["user_id"] = user_id
    if execution_id:
        payload["execution_id"] = execution_id
    if working_dir:
        payload["working_dir"] = working_dir
    with open(event_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")
    try:
        os.chmod(event_file, 0o640)
    except Exception:
        pass


prev = walk_state(watch_path)
write_event(
    {
        "event": "audit-sidecar-started",
        "watch_path": watch_path,
        "audit_dir": audit_dir,
        "excluded_prefixes": exclude_rel_prefixes,
        "file_count": len(prev),
    }
)
with open(state_file, "w", encoding="utf-8") as f:
    json.dump(prev, f, ensure_ascii=True)
try:
    os.chmod(state_file, 0o640)
except Exception:
    pass

while True:
    time.sleep(max(1, interval))
    cur = walk_state(watch_path)
    created = sorted([k for k in cur.keys() if k not in prev])
    deleted = sorted([k for k in prev.keys() if k not in cur])
    modified = sorted(
        [
            k
            for k in cur.keys()
            if k in prev
            and (
                cur[k].get("size") != prev[k].get("size")
                or cur[k].get("mtime") != prev[k].get("mtime")
            )
        ]
    )
    if created or deleted or modified:
        write_event(
            {
                "event": "workspace-change",
                "created": created,
                "deleted": deleted,
                "modified": modified,
                "file_count": len(cur),
            }
        )
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=True)
        try:
            os.chmod(state_file, 0o640)
        except Exception:
            pass
    prev = cur
