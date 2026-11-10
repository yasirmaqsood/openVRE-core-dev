"""Building blocks for interactive session templates ({{ session.* }}), and
the shared helpers for node placement.

The session templates (templates/openvre-*-session.yaml) decide which objects
a session has and how they fit together. The parts that need logic (mounts,
the audit sidecar, labels, Ingress settings) are computed here, so templates
stay declarative and every template gets the same, tested behaviour.
"""

import urllib.parse
from dataclasses import dataclass, field

from . import validation as v

USERDATA_VOLUME = "userdata"
ADMINDATA_VOLUME = "admindata"
AUDIT_CONTAINER = "audit-sidecar"
INTERACTIVE_LABEL = "openvre.io/interactive"
SESSION_LABEL = "openvre.io/session"
TEMPLATE_LABEL = "openvre.io/template"
MANAGED_BY = "openvre-scheduler"
USER_LABEL = "openvre.io/user"
SESSION_ANNOTATION = "openvre.io/session-id"
USER_ANNOTATION = "openvre.io/user-id"
CREATED_ANNOTATION = "openvre.io/created-at"
EXPIRES_ANNOTATION = "openvre.io/expires-at"
AUDIT_ANNOTATION = "openvre.io/audit-host-path"
TEMPLATE_VERSION_ANNOTATION = "openvre.io/template-version"
PROXY_HEADERS_SUFFIX = "-proxy-hdr"

# RStudio writes lots of session metadata under home; keep user files, drop internals.
DEFAULT_AUDIT_EXCLUDE_PREFIXES = ",".join([
    "sessions", "sources", "client-state", "monitored", "pcs", ".local", ".config",
    ".cache", ".rstudio", "bibliography-index", "presentation", "profiles-cache",
    "projects", "projects_settings", "rversion-settings", "tutorial", "ctx", "notebooks",
])

API_PATHS = {
    "ConfigMap": "/api/v1/namespaces/{ns}/configmaps",
    "Deployment": "/apis/apps/v1/namespaces/{ns}/deployments",
    "Service": "/api/v1/namespaces/{ns}/services",
    "Ingress": "/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses",
}


@dataclass
class SessionSpec:
    """One validated session request: the frontend's context plus scheduler-chosen values."""
    name: str
    namespace: str
    session_id: str
    user_id: str
    tool_id: str
    tool_name: str
    project: str
    execution: str
    working_dir: str
    workspace_mount_path: str
    volume_mounts: list
    path_prefix: str
    external_base: str
    template: str
    template_version: str
    access_type: str
    image: str = ""
    port: int = 0
    created_at: str = ""
    expires_at: str = ""
    audit_host_path: str = ""
    access: dict = field(default_factory=dict)


# Node placement (shared with the batch Job template via {{ config.* }}) -------

def parse_node_selector(raw):
    out = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if "=" not in part:
            continue
        k, val = (x.strip() for x in part.split("=", 1))
        if k:
            out[k] = val
    return out


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
            out.append({"key": key.strip(), "operator": "Equal", "value": value.strip(), "effect": effect})
        else:
            out.append({"key": body.strip(), "operator": "Exists", "effect": effect})
    return out


def tolerations(settings):
    eviction = [
        {"key": k, "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": settings.eviction_toleration_seconds}
        for k in ("node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable")
    ]
    return parse_tolerations(settings.tolerations) + eviction


def affinity(settings):
    """Prefer the home worker; also sit with other OpenVRE pods."""
    out = {}
    if settings.preferred_hostname:
        out["nodeAffinity"] = {"preferredDuringSchedulingIgnoredDuringExecution": [{
            "weight": 100,
            "preference": {"matchExpressions": [{
                "key": "kubernetes.io/hostname", "operator": "In", "values": [settings.preferred_hostname]}]},
        }]}
    out["podAffinity"] = {"preferredDuringSchedulingIgnoredDuringExecution": [{
        "weight": 100,
        "podAffinityTerm": {"labelSelector": {"matchLabels": {"openvre.colocate": "true"}},
                            "topologyKey": "kubernetes.io/hostname"},
    }]}
    return out


def external_base_host(external_base):
    base = (external_base or "").strip()
    if not base:
        return None
    if "://" not in base:
        base = f"http://{base}"
    return urllib.parse.urlparse(base).hostname or None


# Session building blocks ----------------------------------------------------

def labels(spec):
    return {
        "app": spec.name,
        INTERACTIVE_LABEL: "true",
        "app.kubernetes.io/managed-by": MANAGED_BY,
        SESSION_LABEL: spec.name,
        USER_LABEL: v.label_value(spec.user_id),
        TEMPLATE_LABEL: spec.template,
    }


def annotations(spec):
    out = {
        SESSION_ANNOTATION: spec.session_id,
        USER_ANNOTATION: spec.user_id,
        CREATED_ANNOTATION: spec.created_at,
        TEMPLATE_VERSION_ANNOTATION: spec.template_version,
    }
    if spec.expires_at:
        out[EXPIRES_ANNOTATION] = spec.expires_at
    if spec.audit_host_path:
        out[AUDIT_ANNOTATION] = spec.audit_host_path
    return out


def userdata_mounts(spec, settings):
    """Mounts of the userData PVC (volume "userdata"). Paths were checked in sessions.py."""
    mounts, seen = [], set()
    for vm in spec.volume_mounts:
        sub = v.subpath(vm["host_path"], settings.shared_data_prefix)
        key = (sub, vm["container_path"])
        if key in seen:
            continue
        seen.add(key)
        m = {"name": USERDATA_VOLUME, "mountPath": vm["container_path"]}
        if sub:
            m["subPath"] = sub
        if vm.get("read_only"):
            m["readOnly"] = True
        mounts.append(m)
    if mounts:
        return mounts
    sub = v.subpath(spec.working_dir, settings.shared_data_prefix)
    m = {"name": USERDATA_VOLUME, "mountPath": spec.workspace_mount_path}
    if sub:
        m["subPath"] = sub
    return [m]


def readiness_probe(port, path):
    """'Running' should mean the app answers, not just that the process started."""
    handler = {"httpGet": {"path": path, "port": port}} if path else {"tcpSocket": {"port": port}}
    return {**handler, "initialDelaySeconds": 3, "periodSeconds": 5, "timeoutSeconds": 3, "failureThreshold": 3}


def audit_blocks(spec, settings, mounts):
    """(containers, volumes) for the audit sidecar, or ([], []) when audit is off."""
    if not (settings.audit_enabled and spec.audit_host_path):
        return [], []
    admin_pvc = settings.admindata_pvc or settings.userdata_pvc
    separate_admin_pvc = admin_pvc != settings.userdata_pvc
    prefix = settings.audit_base if separate_admin_pvc else settings.shared_data_prefix
    sub = v.subpath(spec.audit_host_path, prefix)
    if not sub:
        raise ValueError("audit session subPath is empty")
    container = {
        "name": AUDIT_CONTAINER,
        "image": settings.audit_image,
        "env": [
            {"name": "WATCH_PATH", "value": spec.workspace_mount_path},
            {"name": "WORKING_DIR", "value": spec.working_dir},
            {"name": "EXECUTION_ID", "value": spec.name},
            {"name": "SESSION_ID", "value": spec.session_id},
            {"name": "USER_ID", "value": spec.user_id},
            {"name": "INTERVAL_SEC", "value": settings.audit_interval_sec},
            {"name": "EXCLUDE_REL_PREFIXES", "value": DEFAULT_AUDIT_EXCLUDE_PREFIXES},
        ],
        # userData read-only (watch) + adminData read-write (audit files). The tool container never sees adminData.
        "volumeMounts": [{**m, "readOnly": True} for m in mounts]
        + [{"name": ADMINDATA_VOLUME, "mountPath": "/audit-root", "subPath": sub}],
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": settings.job_run_as_uid,
            "runAsGroup": settings.job_run_as_gid,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {"requests": {"cpu": "10m", "memory": "64Mi"}, "limits": {"cpu": "200m", "memory": "256Mi"}},
    }
    volume = {"name": ADMINDATA_VOLUME, "persistentVolumeClaim": {"claimName": admin_pvc}}
    return [container], [volume]


def proxy_headers(spec, settings, values):
    path = spec.path_prefix.rstrip("/")
    proto = "https" if settings.external_base.startswith("https://") else "http"
    data = {"X-Forwarded-Proto": proto}
    mode = values.get("proxy_root_mode", "")
    if mode == "x_forwarded_prefix":
        data["X-Forwarded-Prefix"] = path
    elif mode == "custom_header" and values.get("proxy_root_header"):
        data[values["proxy_root_header"]] = path
    for k, val in (values.get("proxy_headers") or {}).items():
        data[k] = val.replace("{{PATH_PREFIX}}", path)
    return data


def ingress_annotations(spec, settings):
    path = spec.path_prefix.rstrip("/")
    base = spec.external_base.rstrip("/")
    ann = {
        "nginx.ingress.kubernetes.io/proxy-read-timeout": "3600",
        "nginx.ingress.kubernetes.io/proxy-send-timeout": "3600",
        "nginx.ingress.kubernetes.io/proxy-body-size": "0",
        "nginx.ingress.kubernetes.io/proxy-http-version": "1.1",
        "nginx.ingress.kubernetes.io/rewrite-target": "/$2",
        "nginx.ingress.kubernetes.io/use-regex": "true",
        # Route before the main app ingress catch-all path "/" on the same host.
        "nginx.ingress.kubernetes.io/priority": "100",
    }
    if settings.auth_url:
        ann["nginx.ingress.kubernetes.io/auth-url"] = settings.auth_url
        if settings.auth_signin:
            signin = settings.auth_signin
            if "$escaped_request_uri" not in signin:
                signin += ("&" if "?" in signin else "?") + "rd=$escaped_request_uri"
            ann["nginx.ingress.kubernetes.io/auth-signin"] = signin
    if base:
        ann["nginx.ingress.kubernetes.io/proxy-redirect-from"] = f"{base}/"
        ann["nginx.ingress.kubernetes.io/proxy-redirect-to"] = f"{base}{path}/"
    return ann


def building_blocks(spec, settings, values):
    """Every {{ session.* }} value for one session."""
    mounts = userdata_mounts(spec, settings)
    audit_containers, audit_volumes = audit_blocks(spec, settings, mounts)
    executable = values.get("executable", "")
    pull = [{"name": n} for n in settings.image_pull_secrets]
    pull += [s for s in (values.get("image_pull_secrets") or []) if s["name"] not in settings.image_pull_secrets]
    lbl = labels(spec)
    return {
        "name": spec.name,
        "path": spec.path_prefix,
        "url": spec.path_prefix + "/",
        "namespace": spec.namespace,
        "user_id": spec.user_id,
        "labels": lbl,
        "pod_labels": {**lbl, "openvre.colocate": "true"},
        "annotations": annotations(spec),
        "mounts": mounts,
        "audit_containers": audit_containers,
        "audit_volumes": audit_volumes,
        "readiness_probe": readiness_probe(values["port"], values.get("readiness_path", "")),
        "command": ["sh", "-lc", executable] if executable else None,
        "pull_secrets": pull,
        "proxy_headers": proxy_headers(spec, settings, values),
        "ingress_host": external_base_host(spec.external_base),
        "ingress_path": f"{spec.path_prefix.rstrip('/')}(/|$)(.*)",
        "ingress_annotations": ingress_annotations(spec, settings),
    }
