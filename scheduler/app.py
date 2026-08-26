import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
API_HOST = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
API_PORT = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
DEFAULT_NAMESPACE = os.environ.get("DEFAULT_NAMESPACE", "default")
SCHEDULER_AUTH_TOKEN = os.environ.get("SCHEDULER_AUTH_TOKEN", "")
OPENVRE_INTERACTIVE_AUTH_URL = os.environ.get("OPENVRE_INTERACTIVE_AUTH_URL", "")
OPENVRE_INTERACTIVE_AUTH_SIGNIN = os.environ.get("OPENVRE_INTERACTIVE_AUTH_SIGNIN", "")
OPENVRE_EXTERNAL_BASE = os.environ.get("OPENVRE_EXTERNAL_BASE", "").rstrip("/")
OPENVRE_INGRESS_CLASS = os.environ.get("OPENVRE_INGRESS_CLASS", "").strip()
DEFAULT_SHARED_PVC = os.environ.get("DEFAULT_SHARED_PVC", "").strip()
# User workspaces vs admin audit trees may use different claims (boss mount matrix).
# Both default to the shared PVC so current single-volume clusters keep working.
DEFAULT_USERDATA_PVC = (
    os.environ.get("DEFAULT_USERDATA_PVC", "").strip() or DEFAULT_SHARED_PVC
)
DEFAULT_ADMINDATA_PVC = (
    os.environ.get("DEFAULT_ADMINDATA_PVC", "").strip() or DEFAULT_SHARED_PVC
)
OPENVRE_INTERACTIVE_DEFAULT_PORT = os.environ.get("OPENVRE_INTERACTIVE_DEFAULT_PORT", "").strip()
OPENVRE_INTERACTIVE_DEFAULT_MOUNT = os.environ.get("OPENVRE_INTERACTIVE_DEFAULT_MOUNT", "").strip()
OPENVRE_INTERACTIVE_AUDIT_SIDECAR_ENABLED = (
    os.environ.get("OPENVRE_INTERACTIVE_AUDIT_SIDECAR_ENABLED", "").strip().lower()
)
OPENVRE_INTERACTIVE_AUDIT_IMAGE = os.environ.get(
    "OPENVRE_INTERACTIVE_AUDIT_IMAGE", "python:3.11-alpine"
).strip()
OPENVRE_INTERACTIVE_AUDIT_INTERVAL_SEC = os.environ.get(
    "OPENVRE_INTERACTIVE_AUDIT_INTERVAL_SEC", "5"
).strip()
OPENVRE_INTERACTIVE_AUDIT_BASE = os.environ.get(
    "OPENVRE_INTERACTIVE_AUDIT_BASE", "/shared_data/admindata"
).strip().rstrip("/") or "/shared_data/admindata"
OPENVRE_SHARED_DATA_PREFIX = os.environ.get(
    "OPENVRE_SHARED_DATA_PREFIX", "/shared_data"
).strip().rstrip("/") or "/shared_data"
# Scheduler pod UID for audit YML writes (match OPENVRE_K8S_RUN_AS_UID / tool fsGroup).
AUDIT_RUN_AS_UID = int(os.environ.get("OPENVRE_AUDIT_RUN_AS_UID", "1000") or "1000")
AUDIT_RUN_AS_GID = int(os.environ.get("OPENVRE_AUDIT_RUN_AS_GID", "1000") or "1000")
# Volume names inside interactive pods (never mount admindata into RStudio).
USERDATA_VOLUME = "userdata"
ADMINDATA_VOLUME = "admindata"
INTERACTIVE_LABEL = "openvre.io/interactive"
INTERACTIVE_MANAGED_BY = "openvre-scheduler"
# Optional pin for interactive workloads (e.g. openvre.project=project-a).
# OPENVRE_NODE_SELECTOR: comma-separated key=value pairs
# OPENVRE_TOLERATIONS: comma-separated key[=value]:Effect (Effect default NoSchedule)
# OPENVRE_EVICTION_TOLERATION_SECONDS: overrides Kubernetes 300s not-ready/unreachable wait
OPENVRE_NODE_SELECTOR = os.environ.get("OPENVRE_NODE_SELECTOR", "").strip()
OPENVRE_TOLERATIONS = os.environ.get("OPENVRE_TOLERATIONS", "").strip()
OPENVRE_PREFERRED_HOSTNAME = os.environ.get("OPENVRE_PREFERRED_HOSTNAME", "").strip()
EVICTION_TOLERATION_SECONDS = int(
    os.environ.get("OPENVRE_EVICTION_TOLERATION_SECONDS", "60") or "60"
)


def parse_node_selector(raw):
    out = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out[k] = v
    return out


def interactive_affinity():
    """Prefer the home worker; also sit with other OpenVRE pods in this namespace."""
    affinity = {}
    host = OPENVRE_PREFERRED_HOSTNAME
    if host:
        affinity["nodeAffinity"] = {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "weight": 100,
                    "preference": {
                        "matchExpressions": [
                            {
                                "key": "kubernetes.io/hostname",
                                "operator": "In",
                                "values": [host],
                            }
                        ]
                    },
                }
            ]
        }
    affinity["podAffinity"] = {
        "preferredDuringSchedulingIgnoredDuringExecution": [
            {
                "weight": 100,
                "podAffinityTerm": {
                    "labelSelector": {
                        "matchLabels": {"openvre.colocate": "true"},
                    },
                    "topologyKey": "kubernetes.io/hostname",
                },
            }
        ]
    }
    return affinity


def parse_tolerations(raw):
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        effect = "NoSchedule"
        body = part
        if ":" in part:
            body, effect = part.rsplit(":", 1)
            effect = effect.strip() or "NoSchedule"
        if "=" in body:
            key, value = body.split("=", 1)
            out.append(
                {
                    "key": key.strip(),
                    "operator": "Equal",
                    "value": value.strip(),
                    "effect": effect,
                }
            )
        else:
            out.append({"key": body.strip(), "operator": "Exists", "effect": effect})
    return out


def eviction_tolerations():
    return [
        {
            "key": "node.kubernetes.io/not-ready",
            "operator": "Exists",
            "effect": "NoExecute",
            "tolerationSeconds": EVICTION_TOLERATION_SECONDS,
        },
        {
            "key": "node.kubernetes.io/unreachable",
            "operator": "Exists",
            "effect": "NoExecute",
            "tolerationSeconds": EVICTION_TOLERATION_SECONDS,
        },
    ]


def pod_tolerations():
    return parse_tolerations(OPENVRE_TOLERATIONS) + eviction_tolerations()


with open(TOKEN_PATH, "r", encoding="utf-8") as f:
    SA_TOKEN = f.read().strip()

SSL_CTX = ssl.create_default_context(cafile=CA_PATH)


def k8s_request(method, path, body=None, content_type="application/json"):
    url = f"https://{API_HOST}:{API_PORT}{path}"
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", f"Bearer {SA_TOKEN}")
    if body is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return resp.getcode(), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") if e.fp else str(e)
        return e.code, raw


def sanitize_path_segment(value, max_len=48):
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = value.strip("-")
    if not value:
        value = "user"
    return value[:max_len]


def tool_prefix(tool_id="", tool_name=""):
    raw = tool_id or tool_name or "interactive"
    return sanitize_path_segment(raw, 20)


def sanitize_k8s_name(value, max_len=50):
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = value.strip("-")
    if not value:
        value = "openvre-ix-session"
    if len(value) > max_len:
        value = value[:max_len].rstrip("-")
    return value


def audit_sidecar_enabled():
    return OPENVRE_INTERACTIVE_AUDIT_SIDECAR_ENABLED in (
        "1",
        "true",
        "yes",
        "on",
    )


def host_path_to_subpath(host_path, prefix=None):
    """Convert absolute shared-data path to PVC subPath (e.g. userdata/user/...)."""
    prefix = (prefix or OPENVRE_SHARED_DATA_PREFIX).rstrip("/")
    path = (host_path or "").strip().rstrip("/")
    if not path:
        raise ValueError("host path is required")
    if path == prefix:
        return ""
    if path.startswith(prefix + "/"):
        return path[len(prefix) + 1 :]
    raise ValueError(f"host path {path!r} is not under shared prefix {prefix!r}")


def build_audit_host_path(tool_id, tool_name, user_id, project, execution, working_dir, k8s_name):
    """admindata/<tool>/<user>/<k8s-name>/ — project and run are session metadata only."""
    tool_seg = sanitize_path_segment(tool_id or tool_name or "tool", 48) or "tool"
    user_seg = sanitize_path_segment(user_id, 64) or "user"
    return "/".join(
        [
            OPENVRE_INTERACTIVE_AUDIT_BASE,
            tool_seg,
            user_seg,
            k8s_name,
        ]
    )


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o750)
    except Exception:
        pass
    return path


def write_text_file(path, content):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content if content is not None else "")
    try:
        os.chmod(path, 0o640)
    except Exception:
        pass


def write_json_file(path, payload):
    write_text_file(path, json.dumps(payload, indent=2, ensure_ascii=True) + "\n")


def mark_session_deleted(audit_dir):
    """Update session-meta.json on stop without writing delete-phase live snapshots."""
    if not audit_dir:
        return
    meta_path = os.path.join(audit_dir, "session-meta.json")
    meta = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f) or {}
        except Exception:
            meta = {}
    now = utc_now()
    meta.update(
        {
            "last_phase": "delete",
            "deleted_at": now,
            "last_snapshot_at": now,
        }
    )
    write_json_file(meta_path, meta)


def safe_audit_write(fn, *args, **kwargs):
    """Best-effort audit I/O; never break session create/delete."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"audit write skipped: {e}")
        return None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def list_pods_for_app(ns, app_name):
    q = urllib.parse.urlencode({"labelSelector": f"app={app_name}"})
    code, raw = k8s_request("GET", f"/api/v1/namespaces/{ns}/pods?{q}")
    if code >= 300:
        return []
    try:
        return json.loads(raw).get("items", []) or []
    except Exception:
        return []


def collect_namespace_events(ns, names):
    """Return events whose involvedObject.name is in names."""
    code, raw = k8s_request("GET", f"/api/v1/namespaces/{ns}/events")
    if code >= 300:
        return {"error": raw, "items": []}
    try:
        items = json.loads(raw).get("items", []) or []
    except Exception:
        return {"error": "invalid events json", "items": []}
    name_set = set(names or [])
    matched = [
        ev
        for ev in items
        if (ev.get("involvedObject") or {}).get("name") in name_set
    ]
    matched.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or e.get("metadata", {}).get("creationTimestamp") or "")
    return {"items": matched}


def collect_pod_logs(ns, pod_name, container):
    q = urllib.parse.urlencode({"container": container, "timestamps": "true"})
    code, raw = k8s_request(
        "GET", f"/api/v1/namespaces/{ns}/pods/{pod_name}/log?{q}"
    )
    if code >= 300:
        return f"# failed to fetch logs for {pod_name}/{container}: HTTP {code}\n{raw}\n"
    return raw


def snapshot_k8s_audit(
    audit_dir,
    ns,
    name,
    manifests=None,
    phase="snapshot",
    wait_pods_sec=0,
):
    """Persist K8s manifests/state/logs/events under audit_dir."""
    if not audit_dir:
        return
    try:
        ensure_dir(audit_dir)
    except Exception as e:
        # Scheduler may not have PVC mounted yet; don't fail the session API.
        print(f"audit snapshot mkdir failed: {e}")
        return

    meta_path = os.path.join(audit_dir, "session-meta.json")
    meta = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f) or {}
        except Exception:
            meta = {}
    meta.update(
        {
            "execution_id": name,
            "namespace": ns,
            "last_phase": phase,
            "last_snapshot_at": utc_now(),
        }
    )
    if phase == "create":
        meta["created_at"] = meta.get("created_at") or utc_now()
    if phase == "delete":
        meta["deleted_at"] = utc_now()

    if manifests:
        for manifest in manifests:
            kind = (manifest.get("kind") or "object").lower()
            write_json_file(os.path.join(audit_dir, f"{kind}.json"), manifest)

    if wait_pods_sec > 0:
        deadline = time.time() + wait_pods_sec
        while time.time() < deadline:
            pods = list_pods_for_app(ns, name)
            if pods:
                break
            time.sleep(2)

    pods = list_pods_for_app(ns, name)
    pod_names = [p.get("metadata", {}).get("name", "") for p in pods if p.get("metadata", {}).get("name")]
    if len(pods) == 1:
        write_json_file(os.path.join(audit_dir, f"pod-{phase}.json"), pods[0])

    # Deployment live object
    code, raw = k8s_request(
        "GET", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"
    )
    if code < 300:
        try:
            write_json_file(
                os.path.join(audit_dir, f"deployment-live-{phase}.json"),
                json.loads(raw),
            )
        except Exception:
            write_text_file(
                os.path.join(audit_dir, f"deployment-live-{phase}.json"), raw
            )

    event_names = [name] + pod_names + [f"{name}-proxy-hdr"]
    events = collect_namespace_events(ns, event_names)
    write_json_file(os.path.join(audit_dir, f"k8s-events-{phase}.json"), events)

    write_json_file(meta_path, meta)


def build_userdata_volume_mounts(volume_mounts, workspace_mount_path, working_dir=""):
    """Build userData PVC mounts only (never admindata). Supports readOnly (e.g. repository)."""
    mounts = []
    seen = set()
    for vm in volume_mounts or []:
        host_path = (vm.get("host_path") or "").strip()
        container_path = (vm.get("container_path") or "").strip()
        if not host_path or not container_path:
            continue
        subpath = host_path_to_subpath(host_path)
        key = (subpath, container_path)
        if key in seen:
            continue
        seen.add(key)
        mount = {"name": USERDATA_VOLUME, "mountPath": container_path}
        if subpath:
            mount["subPath"] = subpath
        if vm.get("read_only") or vm.get("readOnly"):
            mount["readOnly"] = True
        mounts.append(mount)

    if mounts:
        return mounts

    if working_dir and workspace_mount_path:
        subpath = host_path_to_subpath(working_dir)
        mount = {"name": USERDATA_VOLUME, "mountPath": workspace_mount_path}
        if subpath:
            mount["subPath"] = subpath
        return [mount]

    return [{"name": USERDATA_VOLUME, "mountPath": workspace_mount_path or "/workspace"}]


# RStudio writes lots of session metadata under home; keep user files, drop internals.
DEFAULT_AUDIT_EXCLUDE_PREFIXES = ",".join(
    [
        "sessions",
        "sources",
        "client-state",
        "monitored",
        "pcs",
        ".local",
        ".config",
        ".cache",
        ".rstudio",
        "bibliography-index",
        "presentation",
        "profiles-cache",
        "projects",
        "projects_settings",
        "rversion-settings",
        "tutorial",
        "ctx",
        "notebooks",
    ]
)


def sidecar_userdata_mounts(pod_volume_mounts):
    """Same userData paths as RStudio, read-only for audit file watching."""
    mounts = []
    for m in pod_volume_mounts or []:
        sm = dict(m)
        sm["readOnly"] = True
        mounts.append(sm)
    return mounts


def deployment_manifest(
    name,
    namespace,
    image,
    path_prefix,
    userdata_pvc,
    admindata_pvc,
    cpu,
    memory,
    container_port,
    executable,
    workspace_mount_path,
    uid,
    gid,
    session_id,
    user_id,
    working_dir="",
    volume_mounts=None,
    audit_host_path="",
):
    """
    Mount matrix:
      RStudio: userData only — repository (ro) + rstudio workspace (rw); NO adminData
      Audit sidecar: userData (ro, watch) + adminData (rw, audit files)
    """
    pod_volume_mounts = build_userdata_volume_mounts(
        volume_mounts, workspace_mount_path, working_dir
    )
    volumes = [
        {
            "name": USERDATA_VOLUME,
            "persistentVolumeClaim": {"claimName": userdata_pvc},
        },
    ]

    container_spec = {
        "name": "interactive",
        "image": image,
        "ports": [{"containerPort": container_port, "name": "http"}],
        "env": [],
        "resources": {
            "requests": {"cpu": cpu, "memory": memory},
            "limits": {"cpu": cpu, "memory": memory},
        },
        # RStudio never sees adminData.
        "volumeMounts": pod_volume_mounts,
    }
    if executable:
        container_spec["command"] = ["sh", "-lc", executable]

    pod_containers = [container_spec]
    audit_root_mount = "/audit-root"
    watch_path = workspace_mount_path
    if audit_sidecar_enabled() and audit_host_path:
        if not any(v["name"] == ADMINDATA_VOLUME for v in volumes):
            volumes.append(
                {
                    "name": ADMINDATA_VOLUME,
                    "persistentVolumeClaim": {"claimName": admindata_pvc},
                }
            )
        # Separate admin PVC style: mount the session leaf itself at /audit-root.
        # AUDIT_DIR is always /audit-root (no nested /audit-root/rstudio/... path).
        # Shared PVC fallback: subPath includes "admindata/..." under the shared claim.
        separate_admin_pvc = (admindata_pvc or "") != (userdata_pvc or "")
        session_subpath = host_path_to_subpath(
            audit_host_path,
            OPENVRE_INTERACTIVE_AUDIT_BASE
            if separate_admin_pvc
            else OPENVRE_SHARED_DATA_PREFIX,
        )
        if not session_subpath:
            raise ValueError("audit session subPath is empty")
        audit_script = r"""
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
"""
        audit_mount = {
            "name": ADMINDATA_VOLUME,
            "mountPath": audit_root_mount,
            "subPath": session_subpath,
        }
        audit_container = {
            "name": "audit-sidecar",
            "image": OPENVRE_INTERACTIVE_AUDIT_IMAGE,
            "command": ["python3", "-u", "-c", audit_script],
            "env": [
                {"name": "WATCH_PATH", "value": watch_path},
                {"name": "WORKING_DIR", "value": working_dir},
                {"name": "EXECUTION_ID", "value": name},
                {"name": "SESSION_ID", "value": session_id},
                {"name": "USER_ID", "value": user_id},
                {"name": "INTERVAL_SEC", "value": OPENVRE_INTERACTIVE_AUDIT_INTERVAL_SEC},
                {
                    "name": "EXCLUDE_REL_PREFIXES",
                    "value": DEFAULT_AUDIT_EXCLUDE_PREFIXES,
                },
            ],
            # userData ro (watch) + adminData rw (audit writes). RStudio still has no adminData.
            "volumeMounts": sidecar_userdata_mounts(pod_volume_mounts) + [audit_mount],
            # Same UID/GID as tool container (run_as_uid/gid from session payload).
            "securityContext": {
                "runAsNonRoot": int(uid) != 0,
                "runAsUser": int(uid),
                "runAsGroup": int(gid),
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
            "resources": {
                "requests": {"cpu": "10m", "memory": "64Mi"},
                "limits": {"cpu": "200m", "memory": "256Mi"},
            },
        }
        pod_containers.append(audit_container)

    dep_annotations = {}
    if audit_host_path:
        dep_annotations["openvre.io/audit-host-path"] = audit_host_path

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app": name,
                INTERACTIVE_LABEL: "true",
                "app.kubernetes.io/managed-by": INTERACTIVE_MANAGED_BY,
            },
            **({"annotations": dep_annotations} if dep_annotations else {}),
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {
                    "labels": {
                        "app": name,
                        INTERACTIVE_LABEL: "true",
                        "openvre.colocate": "true",
                    }
                },
                "spec": {
                    # RStudio/rocker s6 init must start as root; only fsGroup for PVC ownership.
                    # run_as_uid is accepted for API compatibility but not applied as runAsUser.
                    "securityContext": {"fsGroup": max(1, int(gid))},
                    "containers": pod_containers,
                    "volumes": volumes,
                    **(
                        {"nodeSelector": parse_node_selector(OPENVRE_NODE_SELECTOR)}
                        if parse_node_selector(OPENVRE_NODE_SELECTOR)
                        else {}
                    ),
                    "affinity": interactive_affinity(),
                    "tolerations": pod_tolerations(),
                },
            },
        },
    }


def service_manifest(name, namespace, container_port):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {INTERACTIVE_LABEL: "true"},
        },
        "spec": {
            "selector": {"app": name},
            "ports": [{"name": "http", "port": container_port, "targetPort": container_port}],
        },
    }


def external_base_host(external_base):
    """Hostname from OPENVRE_EXTERNAL_BASE (e.g. http://openvre.local -> openvre.local)."""
    base = (external_base or "").strip()
    if not base:
        return None
    if "://" not in base:
        base = f"http://{base}"
    return urllib.parse.urlparse(base).hostname or None


def ingress_manifest(
    name, namespace, path_prefix, service_name, external_base, container_port
):
    path = path_prefix if path_prefix.startswith("/") else f"/{path_prefix}"
    path = path.rstrip("/")
    regex_path = f"{path}(/|$)(.*)"
    base = (external_base or "").rstrip("/")
    ingress_host = external_base_host(base)
    annotations = {
        "nginx.ingress.kubernetes.io/proxy-read-timeout": "3600",
        "nginx.ingress.kubernetes.io/proxy-send-timeout": "3600",
        "nginx.ingress.kubernetes.io/proxy-body-size": "0",
        "nginx.ingress.kubernetes.io/proxy-http-version": "1.1",
        "nginx.ingress.kubernetes.io/rewrite-target": "/$2",
        "nginx.ingress.kubernetes.io/use-regex": "true",
    }
    annotations["nginx.ingress.kubernetes.io/proxy-set-headers"] = (
        f"{namespace}/{name}-proxy-hdr"
    )
    # Route before the main app ingress catch-all path "/" on the same host.
    annotations["nginx.ingress.kubernetes.io/priority"] = "100"
    if OPENVRE_INTERACTIVE_AUTH_URL:
        annotations["nginx.ingress.kubernetes.io/auth-url"] = OPENVRE_INTERACTIVE_AUTH_URL
        if OPENVRE_INTERACTIVE_AUTH_SIGNIN:
            signin = OPENVRE_INTERACTIVE_AUTH_SIGNIN
            if "$escaped_request_uri" not in signin:
                sep = "&" if "?" in signin else "?"
                signin = f"{signin}{sep}rd=$escaped_request_uri"
            annotations["nginx.ingress.kubernetes.io/auth-signin"] = signin
    if base:
        annotations["nginx.ingress.kubernetes.io/proxy-redirect-from"] = (
            f"{base}/"
        )
        annotations["nginx.ingress.kubernetes.io/proxy-redirect-to"] = (
            f"{base}{path}/"
        )
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {INTERACTIVE_LABEL: "true"},
            "annotations": annotations,
        },
        "spec": {
            "ingressClassName": OPENVRE_INGRESS_CLASS,
            "rules": [
                {
                    **({"host": ingress_host} if ingress_host else {}),
                    "http": {
                        "paths": [
                            {
                                "path": regex_path,
                                "pathType": "ImplementationSpecific",
                                "backend": {
                                    "service": {
                                        "name": service_name,
                                        "port": {"number": container_port},
                                    }
                                },
                            }
                        ]
                    },
                }
            ],
        },
    }


def proxy_headers_manifest(name, namespace, path_prefix, proxy_cfg=None):
    path = path_prefix if path_prefix.startswith("/") else f"/{path_prefix}"
    path = path.rstrip("/")
    proxy_cfg = proxy_cfg or {}
    mode = str(proxy_cfg.get("root_path_mode", "")).strip().lower()
    header_name = str(proxy_cfg.get("root_path_header", "")).strip()
    extra_headers = proxy_cfg.get("extra_headers", {}) or {}

    # Prefer scheme from OPENVRE_EXTERNAL_BASE; default http only if unset.
    proto = "http"
    if OPENVRE_EXTERNAL_BASE:
        if OPENVRE_EXTERNAL_BASE.startswith("https://"):
            proto = "https"
        elif OPENVRE_EXTERNAL_BASE.startswith("http://"):
            proto = "http"
    data = {"X-Forwarded-Proto": proto}
    if mode == "x_forwarded_prefix":
        data["X-Forwarded-Prefix"] = path
    elif mode == "custom_header" and header_name:
        data[header_name] = path
    elif mode == "none":
        pass

    for k, v in extra_headers.items():
        if not k:
            continue
        sval = str(v).replace("{{PATH_PREFIX}}", path)
        data[str(k)] = sval

    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{name}-proxy-hdr",
            "namespace": namespace,
            "labels": {INTERACTIVE_LABEL: "true"},
        },
        "data": data,
    }


class Handler(BaseHTTPRequestHandler):
    def _authorized(self):
        if SCHEDULER_AUTH_TOKEN == "":
            return False
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {SCHEDULER_AUTH_TOKEN}"

    def _json(self, code, payload):
        out = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"ok": True})
        if not self._authorized():
            return self._json(401, {"ok": False, "error": "unauthorized"})

        if self.path.startswith("/jobs/"):
            name_q = self.path[len("/jobs/") :]
            name, _, query = name_q.partition("?")
            params = urllib.parse.parse_qs(query)
            ns = params.get("namespace", [DEFAULT_NAMESPACE])[0]
            code, raw = k8s_request("GET", f"/apis/batch/v1/namespaces/{ns}/jobs/{name}")
            if code == 404:
                return self._json(200, {"ok": True, "exists": False, "job": ""})
            if code >= 300:
                return self._json(500, {"ok": False, "error": raw})
            return self._json(200, {"ok": True, "exists": True, "job": raw})

        if self.path.startswith("/interactive-sessions/"):
            name_q = self.path[len("/interactive-sessions/") :]
            name, _, query = name_q.partition("?")
            params = urllib.parse.parse_qs(query)
            ns = params.get("namespace", [DEFAULT_NAMESPACE])[0]
            code, raw = k8s_request(
                "GET", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"
            )
            if code == 404:
                return self._json(200, {"ok": True, "exists": False, "deployment": ""})
            if code >= 300:
                return self._json(500, {"ok": False, "error": raw})
            dep = json.loads(raw)
            status = dep.get("status", {})
            ready = status.get("readyReplicas", 0) or 0
            state = "Running" if ready >= 1 else "Pending"
            return self._json(
                200,
                {
                    "ok": True,
                    "exists": True,
                    "state": state,
                    "deployment": raw,
                },
            )

        return self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._json(401, {"ok": False, "error": "unauthorized"})

        if self.path == "/jobs":
            try:
                body = self._read_json()
                ns = body.get("namespace") or DEFAULT_NAMESPACE
                manifest = body.get("manifest")
                if not manifest:
                    return self._json(400, {"ok": False, "error": "manifest is required"})
                code, raw = k8s_request(
                    "POST",
                    f"/apis/batch/v1/namespaces/{ns}/jobs",
                    body=manifest,
                    content_type="application/yaml",
                )
                if code >= 300:
                    return self._json(500, {"ok": False, "error": raw})
                return self._json(200, {"ok": True, "stdout": raw, "stderr": ""})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if self.path == "/interactive-sessions":
            try:
                body = self._read_json()
                ns = body.get("namespace") or DEFAULT_NAMESPACE
                user_id = body.get("user_id", "")
                session_id = body.get("session_id") or ""
                image = body.get("image") or os.environ.get("OPENVRE_INTERACTIVE_DEFAULT_IMAGE", "")
                pvc = (body.get("pvc") or DEFAULT_SHARED_PVC or "").strip()
                userdata_pvc = (
                    body.get("userdata_pvc")
                    or body.get("user_data_pvc")
                    or DEFAULT_USERDATA_PVC
                    or pvc
                ).strip()
                admindata_pvc = (
                    body.get("admindata_pvc")
                    or body.get("admin_data_pvc")
                    or DEFAULT_ADMINDATA_PVC
                    or pvc
                ).strip()
                cpu = body.get("cpu", "500m")
                memory = body.get("memory", "1536Mi")
                executable = (body.get("executable") or "").strip()
                workspace_mount_path = (
                    body.get("workspace_mount_path") or OPENVRE_INTERACTIVE_DEFAULT_MOUNT or ""
                ).strip()
                working_dir = (body.get("working_dir") or "").strip()
                volume_mounts = body.get("volume_mounts") or []
                project = (body.get("project") or "").strip()
                execution = (body.get("execution") or "").strip()
                audit_host_path = (body.get("audit_host_path") or "").strip()
                proxy_cfg = body.get("proxy", {}) or {}
                container_port = body.get("container_port")
                if container_port is None or container_port == "":
                    container_port = OPENVRE_INTERACTIVE_DEFAULT_PORT or None
                tool_id = body.get("tool_id", "")
                tool_name = body.get("tool_name", "")
                run_as_uid = body.get("run_as_uid")
                run_as_gid = body.get("run_as_gid")
                external_base = (body.get("external_base") or OPENVRE_EXTERNAL_BASE).rstrip("/")

                if not user_id:
                    return self._json(400, {"ok": False, "error": "user_id is required"})
                if not image:
                    return self._json(400, {"ok": False, "error": "image is required"})
                if not userdata_pvc:
                    return self._json(
                        400,
                        {
                            "ok": False,
                            "error": "userdata_pvc is required (body, DEFAULT_USERDATA_PVC, or pvc/DEFAULT_SHARED_PVC)",
                        },
                    )
                if audit_sidecar_enabled() and not admindata_pvc:
                    return self._json(
                        400,
                        {
                            "ok": False,
                            "error": "admindata_pvc is required when audit sidecar is enabled",
                        },
                    )
                if not workspace_mount_path:
                    return self._json(400, {"ok": False, "error": "workspace_mount_path is required (body or OPENVRE_INTERACTIVE_DEFAULT_MOUNT)"})
                if not working_dir:
                    return self._json(400, {"ok": False, "error": "working_dir is required"})
                if container_port is None:
                    return self._json(400, {"ok": False, "error": "container_port is required (body or OPENVRE_INTERACTIVE_DEFAULT_PORT)"})
                if not OPENVRE_INGRESS_CLASS:
                    return self._json(400, {"ok": False, "error": "OPENVRE_INGRESS_CLASS is not set"})
                if run_as_uid is None or run_as_gid is None:
                    return self._json(400, {"ok": False, "error": "run_as_uid and run_as_gid are required"})
                container_port = int(container_port)
                run_as_uid = int(run_as_uid)
                run_as_gid = int(run_as_gid)
                if not session_id:
                    session_id = f"{tool_prefix(tool_id, tool_name)}-{os.urandom(4).hex()}"

                session_seg = sanitize_path_segment(session_id, 32)
                path_prefix = f"/interactive-tool/{session_seg}"
                name = sanitize_k8s_name(f"openvre-ix-{session_seg}")

                # Admin-only audit tree (outside userdata). Leaf = K8s deployment name.
                if not audit_host_path:
                    audit_host_path = build_audit_host_path(
                        tool_id,
                        tool_name,
                        user_id,
                        project,
                        execution,
                        working_dir,
                        name,
                    )
                # Session leaf must exist before sidecar mounts it via PVC subPath.
                if audit_sidecar_enabled() and audit_host_path:
                    ensure_dir(audit_host_path)

                manifests = [
                    proxy_headers_manifest(name, ns, path_prefix, proxy_cfg),
                    deployment_manifest(
                        name,
                        ns,
                        image,
                        path_prefix,
                        userdata_pvc,
                        admindata_pvc or userdata_pvc,
                        cpu,
                        memory,
                        container_port,
                        executable,
                        workspace_mount_path,
                        run_as_uid,
                        run_as_gid,
                        session_id,
                        user_id,
                        working_dir,
                        volume_mounts,
                        audit_host_path if audit_sidecar_enabled() else "",
                    ),
                ]
                manifests.extend([
                    service_manifest(name, ns, container_port),
                    ingress_manifest(
                        name, ns, path_prefix, name, external_base, container_port
                    ),
                ])

                for manifest in manifests:
                    kind = manifest["kind"]
                    api_path = {
                        "ConfigMap": f"/api/v1/namespaces/{ns}/configmaps",
                        "Deployment": f"/apis/apps/v1/namespaces/{ns}/deployments",
                        "Service": f"/api/v1/namespaces/{ns}/services",
                        "Ingress": f"/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses",
                    }[kind]
                    code, raw = k8s_request(
                        "POST",
                        api_path,
                        body=json.dumps(manifest),
                        content_type="application/json",
                    )
                    if code >= 300 and code != 409:
                        self._delete_interactive(name, ns, snapshot=False)
                        return self._json(
                            500,
                            {
                                "ok": False,
                                "error": f"failed to create {kind}: {raw}",
                            },
                        )

                access_url = path_prefix + "/"
                if audit_sidecar_enabled() and audit_host_path:
                    # Immediate: save requested manifests + meta only.
                    # Live K8s snapshots are taken once in the "running" phase.
                    def _write_create_audit():
                        write_json_file(
                            os.path.join(audit_host_path, "session-meta.json"),
                            {
                                "execution_id": name,
                                "session_id": session_id,
                                "user_id": user_id,
                                "tool_id": tool_id,
                                "tool_name": tool_name,
                                "project": project,
                                "execution": execution,
                                "working_dir": working_dir,
                                "namespace": ns,
                                "image": image,
                                "access_url": access_url,
                                "path_prefix": path_prefix,
                                "audit_host_path": audit_host_path,
                                "created_at": utc_now(),
                                "last_phase": "create",
                            },
                        )
                        for manifest in manifests:
                            kind = (manifest.get("kind") or "object").lower()
                            write_json_file(
                                os.path.join(audit_host_path, f"{kind}.json"),
                                manifest,
                            )

                    safe_audit_write(_write_create_audit)

                    # Deferred: wait for pod and capture live deployment/pod/events.
                    def _bg():
                        safe_audit_write(
                            snapshot_k8s_audit,
                            audit_host_path,
                            ns,
                            name,
                            None,
                            "running",
                            90,
                        )

                    threading.Thread(target=_bg, daemon=True).start()

                return self._json(
                    200,
                    {
                        "ok": True,
                        "name": name,
                        "session_id": session_id,
                        "user_path": "",
                        "access_url": access_url,
                        "path_prefix": path_prefix,
                        "audit_host_path": audit_host_path if audit_sidecar_enabled() else "",
                    },
                )
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        return self._json(404, {"ok": False, "error": "not found"})

    def _resolve_audit_path_for_name(self, name, ns):
        code, raw = k8s_request(
            "GET", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"
        )
        if code >= 300:
            return ""
        try:
            dep = json.loads(raw)
        except Exception:
            return ""
        return (
            (dep.get("metadata") or {}).get("annotations") or {}
        ).get("openvre.io/audit-host-path", "")

    def _delete_interactive(self, name, ns, snapshot=True):
        # Update session-meta to deleted, but do not write delete-phase live files.
        if snapshot and audit_sidecar_enabled():
            audit_dir = self._resolve_audit_path_for_name(name, ns)
            if audit_dir:
                safe_audit_write(mark_session_deleted, audit_dir)
        for kind, path in [
            ("Ingress", f"/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses/{name}"),
            ("Service", f"/api/v1/namespaces/{ns}/services/{name}"),
            ("Deployment", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"),
            ("ConfigMap", f"/api/v1/namespaces/{ns}/configmaps/{name}-proxy-hdr"),
        ]:
            k8s_request("DELETE", path)

    def do_DELETE(self):
        if not self._authorized():
            return self._json(401, {"ok": False, "error": "unauthorized"})

        if self.path.startswith("/interactive-sessions/"):
            name_q = self.path[len("/interactive-sessions/") :]
            name, _, query = name_q.partition("?")
            params = urllib.parse.parse_qs(query)
            ns = params.get("namespace", [DEFAULT_NAMESPACE])[0]
            self._delete_interactive(name, ns, snapshot=True)
            return self._json(200, {"ok": True, "stdout": f"deleted {name}", "stderr": ""})

        if not self.path.startswith("/jobs/"):
            return self._json(404, {"ok": False, "error": "not found"})

        name_q = self.path[len("/jobs/") :]
        name, _, query = name_q.partition("?")
        params = urllib.parse.parse_qs(query)
        ns = params.get("namespace", [DEFAULT_NAMESPACE])[0]
        delete_opts = (
            '{"apiVersion":"batch/v1","kind":"DeleteOptions","propagationPolicy":"Background"}'
        )
        code, raw = k8s_request(
            "DELETE",
            f"/apis/batch/v1/namespaces/{ns}/jobs/{name}",
            body=delete_opts,
            content_type="application/json",
        )
        if code == 404:
            return self._json(
                200,
                {"ok": True, "stdout": f"job.batch/{name} already deleted", "stderr": ""},
            )
        if code >= 300:
            return self._json(500, {"ok": False, "error": raw})
        return self._json(200, {"ok": True, "stdout": raw, "stderr": ""})

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8080), Handler)
    server.serve_forever()
