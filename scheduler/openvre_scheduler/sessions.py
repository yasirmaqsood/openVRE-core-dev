"""Interactive sessions: create from a template, read status, delete, and expire.

A request names a session template and carries two parts:
  context  set by the frontend: who and where (user, tool, project, run folder,
           workspace mounts). Checked here by fixed rules.
  params   the tool's own settings (image, port, env, ...). Checked by the
           template's declared parameter types.
"""

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from . import audit
from . import manifests as mf
from . import policy
from . import state
from . import validation as v
from .k8s import K8sError, q
from .templates import SessionTemplate

log = logging.getLogger("openvre_scheduler.sessions")

_create_lock = threading.Lock()

CONTEXT_KEYS = {"user_id", "tool_id", "tool_name", "session_id", "project", "execution",
                "working_dir", "workspace_mount_path", "volume_mounts", "external_base"}


def _now():
    return datetime.now(timezone.utc)


def parse_request(body, settings, templates):
    """Validate a POST /interactive-sessions body.
    Returns (template, SessionSpec, resolved params)."""
    v.require(isinstance(body, dict), "request body must be a JSON object")
    ns = v.namespace(body.get("namespace"), settings)

    tname = v.plain_text(body.get("template"), "template", 63, required=True)
    tpl = templates.get(tname)
    v.require(tpl is not None, f"unknown template {tname!r}", 404)
    v.require(isinstance(tpl, SessionTemplate), f"template {tname!r} is not an InteractiveSession template")

    ctx = body.get("context") or {}
    v.require(isinstance(ctx, dict), "context must be a JSON object")
    unknown = set(ctx) - CONTEXT_KEYS
    v.require(not unknown, f"unknown context fields: {sorted(unknown)}")

    values = tpl.resolve_params(body.get("params") or {}, settings)
    if "cpu" in values:
        v.at_most(values["cpu"], settings.max_session_cpu, "cpu")
    if "memory" in values:
        v.at_most(values["memory"], settings.max_session_memory, "memory")

    user_id = v.plain_text(ctx.get("user_id"), "context.user_id", 128, required=True)
    tool_id = v.plain_text(ctx.get("tool_id"), "context.tool_id", 128)
    tool_name = v.plain_text(ctx.get("tool_name"), "context.tool_name", 128)
    session_id = v.plain_text(ctx.get("session_id"), "context.session_id", 128)
    project = v.plain_text(ctx.get("project"), "context.project", 256)
    execution = v.plain_text(ctx.get("execution"), "context.execution", 256)

    prefix = settings.shared_data_prefix.rstrip("/")
    audit_base = settings.audit_base.rstrip("/")

    def user_path(value, what):
        # Strictly below the shared prefix (never its root, which would expose every
        # user's data) and never inside the admin audit tree.
        path = v.path_under(value, prefix, what)
        v.require(path != prefix, f"{what} must be a folder below {prefix}", 403)
        v.require(not (path == audit_base or path.startswith(audit_base + "/")), f"{what} must not be inside {audit_base}", 403)
        return path

    working_dir = user_path(ctx.get("working_dir"), "context.working_dir")
    mount_path = v.abs_path(ctx.get("workspace_mount_path") or settings.interactive_default_mount or "/workspace",
                            "context.workspace_mount_path")
    raw_mounts = ctx.get("volume_mounts") or []
    v.require(isinstance(raw_mounts, list) and len(raw_mounts) <= 20, "context.volume_mounts must be a list of at most 20 items")
    mounts = []
    for i, vm in enumerate(raw_mounts):
        v.require(isinstance(vm, dict), f"context.volume_mounts[{i}] must be an object")
        mounts.append({
            "host_path": user_path(vm.get("host_path"), f"context.volume_mounts[{i}].host_path"),
            "container_path": v.abs_path(vm.get("container_path"), f"context.volume_mounts[{i}].container_path"),
            "read_only": bool(vm.get("read_only") or vm.get("readOnly")),
        })

    if tpl.has_ingress:
        v.require(settings.ingress_class, "OPENVRE_INGRESS_CLASS is not set on the scheduler", 500)

    # The scheduler picks the name. The frontend's session_id is kept as metadata only,
    # so two requests can never land on the same objects.
    name = f"{v.SESSION_PREFIX}{v.segment(tool_id or tool_name, 16, 'tool')}-{os.urandom(6).hex()}"
    path_prefix = f"/interactive-tool/{name[len(v.SESSION_PREFIX):]}"

    # The browser-facing host comes from the scheduler config. The request value is
    # a fallback only, because the frontend fills it from the HTTP Host header.
    external_base = settings.external_base or v.plain_text(ctx.get("external_base"), "context.external_base", 256).rstrip("/")

    created = _now()
    expires = created + timedelta(hours=settings.session_ttl_hours) if settings.session_ttl_hours else None
    spec = mf.SessionSpec(
        name=name, namespace=ns, session_id=session_id or name, user_id=user_id,
        tool_id=tool_id, tool_name=tool_name, project=project, execution=execution,
        working_dir=working_dir, workspace_mount_path=mount_path, volume_mounts=mounts,
        path_prefix=path_prefix, external_base=external_base,
        template=tpl.name, template_version=tpl.version, access_type=tpl.access["type"],
        image=str(values.get("image", "")), port=values["port"],
        created_at=created.isoformat(), expires_at=expires.isoformat() if expires else "",
    )
    return tpl, spec, values


def build(tpl, spec, values, settings):
    """Fill the template and run the session policy. Returns (objects, access)."""
    blocks = mf.building_blocks(spec, settings, values)
    objects, access = tpl.render(values, settings, blocks)
    objects, errors = policy.check_session_objects(objects, spec, blocks, settings, log)
    if errors:
        raise v.ValidationError(f"session rejected by policy ({tpl.name}): " + "; ".join(errors), 422)
    if access["type"] == "guacamole":
        access = {"type": "guacamole", "host": spec.name, "port": spec.port,
                  "protocol": str(access.get("protocol", "vnc"))}
    else:
        access = {"type": "web", "url": spec.path_prefix + "/"}
    return objects, access


def count_user_sessions(kube, ns, user_id):
    selector = f"{mf.INTERACTIVE_LABEL}=true,{mf.USER_LABEL}={v.label_value(user_id)}"
    return len(kube.list(f"/apis/apps/v1/namespaces/{q(ns)}/deployments", selector))


def create(kube, settings, tpl, spec, values):
    """Create every object of a session. On failure, delete only what this call created."""
    with _create_lock:
        if settings.max_sessions_per_user:
            n = count_user_sessions(kube, spec.namespace, spec.user_id)
            v.require(n < settings.max_sessions_per_user,
                      f"session limit reached ({settings.max_sessions_per_user} per user); stop a running session first", 429)

        if settings.audit_enabled:
            spec.audit_host_path = audit.session_dir(settings, spec.tool_id, spec.tool_name, spec.user_id, spec.name)

        objects, access = build(tpl, spec, values, settings)
        spec.access = access

        if spec.audit_host_path:
            # The sidecar mounts this folder via subPath, so it must exist first.
            audit.ensure_dir(spec.audit_host_path)

        created = []
        try:
            for obj in objects:
                kube.create(mf.API_PATHS[obj["kind"]].format(ns=q(spec.namespace)), obj)
                created.append(obj)
        except K8sError:
            for obj in reversed(created):
                try:
                    kube.delete(_object_path(obj["kind"], spec.namespace, obj["metadata"]["name"]))
                except K8sError as e:
                    log.error("rollback delete failed", extra={"kind": obj["kind"], "name": obj["metadata"]["name"], "error": e.message})
            raise

    if spec.audit_host_path:
        audit.safe(audit.write_create, spec.audit_host_path, spec, objects)
        # Follow the session until it is ready or fails, and record why if it fails.
        threading.Thread(
            target=audit.safe, args=(audit.watch_session, kube, settings, spec.audit_host_path, spec.namespace, spec.name),
            name=f"audit-{spec.name}", daemon=True,
        ).start()
    return objects


def _object_path(kind, ns, name):
    return mf.API_PATHS[kind].format(ns=q(ns)) + "/" + q(name)


def _is_managed(obj):
    labels = (obj or {}).get("metadata", {}).get("labels") or {}
    return labels.get(mf.INTERACTIVE_LABEL) == "true"


def status(kube, ns, name):
    """Return None if the session does not exist, else a dict with state, reason and message."""
    dep = kube.get(_object_path("Deployment", ns, name))
    if dep is None or not _is_managed(dep):
        return None
    pods = [] if ((dep.get("status") or {}).get("readyReplicas") or 0) >= 1 else \
        kube.list(f"/api/v1/namespaces/{q(ns)}/pods", f"app={name}")
    return {**state.evaluate(dep, pods), "deployment": dep}


def _session_objects(kube, ns, name):
    """(kind, name, object) for every object of a session: found by the session
    label (template sessions), plus the fixed names older sessions used."""
    found = {}
    for kind, base in mf.API_PATHS.items():
        for obj in kube.list(base.format(ns=q(ns)), f"{mf.SESSION_LABEL}={name}"):
            found[(kind, obj["metadata"]["name"])] = obj
    for kind, obj_name in (("Ingress", name), ("Service", name), ("Deployment", name),
                           ("ConfigMap", name + mf.PROXY_HEADERS_SUFFIX)):
        if (kind, obj_name) not in found:
            obj = kube.get(_object_path(kind, ns, obj_name))
            if obj is not None:
                found[(kind, obj_name)] = obj
    return found


def delete(kube, settings, ns, name, reason="delete"):
    """Delete a session's objects. Returns (found, errors). Objects without the
    interactive label are left alone, so this cannot delete platform objects."""
    errors = []
    try:
        objs = _session_objects(kube, ns, name)
    except K8sError as e:
        return False, [e.message]
    dep = objs.get(("Deployment", name))
    if dep is not None and _is_managed(dep):
        audit_dir = (dep.get("metadata", {}).get("annotations") or {}).get(mf.AUDIT_ANNOTATION, "")
        if settings.audit_enabled and audit_dir:
            # Final state, events and logs, before the objects (and their logs) disappear.
            audit.safe(audit.snapshot_delete, kube, settings, audit_dir, ns, name, reason)
    found = False
    # Ingress first so traffic stops before the backend disappears.
    order = {"Ingress": 0, "Service": 1, "Deployment": 2, "ConfigMap": 3}
    for (kind, obj_name), obj in sorted(objs.items(), key=lambda kv: order[kv[0][0]]):
        if not _is_managed(obj):
            log.warning("refusing to delete unmanaged object", extra={"kind": kind, "name": obj_name})
            continue
        found = True
        try:
            kube.delete(_object_path(kind, ns, obj_name))
        except K8sError as e:
            errors.append(f"{kind} {obj_name}: {e.message}")
    return found, errors


def expired(dep, now=None):
    raw = (dep.get("metadata", {}).get("annotations") or {}).get(mf.EXPIRES_ANNOTATION)
    if not raw:
        return False
    try:
        return datetime.fromisoformat(raw) <= (now or _now())
    except ValueError:
        return False


def reap_once(kube, settings):
    """Delete sessions whose expires-at has passed. Returns the names deleted."""
    deleted = []
    selector = f"{mf.INTERACTIVE_LABEL}=true,app.kubernetes.io/managed-by={mf.MANAGED_BY}"
    for ns in settings.allowed_namespaces:
        for dep in kube.list(f"/apis/apps/v1/namespaces/{q(ns)}/deployments", selector):
            if expired(dep):
                name = dep["metadata"]["name"]
                _, errors = delete(kube, settings, ns, name, reason="expired")
                if errors:
                    log.error("expired session delete failed", extra={"name": name, "errors": errors})
                else:
                    log.info("expired session deleted", extra={"name": name, "namespace": ns})
                    deleted.append(name)
    return deleted


def start_reaper(kube, settings, stop_event):
    if not settings.session_ttl_hours:
        return None

    def loop():
        while not stop_event.wait(settings.reaper_interval_sec):
            try:
                reap_once(kube, settings)
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                log.error("session reaper failed", extra={"error": str(e)})

    t = threading.Thread(target=loop, name="session-reaper", daemon=True)
    t.start()
    return t
