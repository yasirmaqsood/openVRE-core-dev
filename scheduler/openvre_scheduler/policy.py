"""Policy for batch Job manifests sent to POST /jobs.

The frontend builds the Job. The scheduler only accepts it if it stays inside
these rules, and it forces safe settings on top. The goal: holding the
scheduler token must not be enough to run privileged pods, read Secrets or
borrow a ServiceAccount.
"""

import copy

from . import validation as v

ALLOWED_VOLUME_TYPES = {"persistentVolumeClaim", "emptyDir"}
FORBIDDEN_POD_FIELDS = (
    "hostNetwork", "hostPID", "hostIPC", "hostUsers", "nodeName",
    "serviceAccountName", "serviceAccount", "initContainers",
    "ephemeralContainers", "shareProcessNamespace", "hostAliases",
    "runtimeClassName", "priorityClassName", "overhead",
)
FORBIDDEN_CONTAINER_FIELDS = ("envFrom", "volumeDevices", "lifecycle")


def _violations_container(c, idx, settings, log):
    errs = []
    where = f"containers[{idx}]"
    if not isinstance(c, dict):
        return [f"{where} must be an object"]
    try:
        v.image(c.get("image"), settings, log)
    except v.ValidationError as e:
        errs.append(f"{where}: {e.message}")
    for f in FORBIDDEN_CONTAINER_FIELDS:
        if f in c:
            errs.append(f"{where}.{f} is not allowed")
    for i, env in enumerate(c.get("env") or []):
        if isinstance(env, dict) and "valueFrom" in env:
            src = env["valueFrom"] or {}
            if "secretKeyRef" in src or "configMapKeyRef" in src:
                errs.append(f"{where}.env[{i}] must not read Secrets or ConfigMaps")
    sc = c.get("securityContext") or {}
    if sc.get("privileged"):
        errs.append(f"{where}: privileged is not allowed")
    if (sc.get("capabilities") or {}).get("add"):
        errs.append(f"{where}: adding capabilities is not allowed")
    if sc.get("runAsUser") == 0:
        errs.append(f"{where}: running as root is not allowed")
    for i, m in enumerate(c.get("volumeMounts") or []):
        if isinstance(m, dict) and ".." in str(m.get("subPath", "")).split("/"):
            errs.append(f"{where}.volumeMounts[{i}].subPath must not contain '..'")
    return errs


def check_job(manifest, ns, settings, log=None):
    """Return (job, errors). job is a copy of the manifest with safe settings forced."""
    if not isinstance(manifest, dict):
        return None, ["manifest must be a JSON object"]
    job = copy.deepcopy(manifest)
    errs = []

    if job.get("apiVersion") != "batch/v1" or job.get("kind") != "Job":
        errs.append("manifest must be apiVersion batch/v1, kind Job")
    meta = job.setdefault("metadata", {})
    try:
        v.job_name(meta.get("name"))
    except v.ValidationError as e:
        errs.append(e.message)
    if meta.get("namespace") not in (None, "", ns):
        errs.append("metadata.namespace must match the request namespace")
    meta["namespace"] = ns
    for f in ("generateName", "ownerReferences", "finalizers"):
        meta.pop(f, None)

    spec = job.setdefault("spec", {})
    for f in ("selector", "manualSelector", "podFailurePolicy", "suspend"):
        spec.pop(f, None)
    tmpl = spec.setdefault("template", {})
    pod = tmpl.setdefault("spec", {})

    for f in FORBIDDEN_POD_FIELDS:
        if f in pod and pod.get(f) not in (None, False, "", [], "default"):
            errs.append(f"pod spec field {f} is not allowed")
        pod.pop(f, None)

    for i, vol in enumerate(pod.get("volumes") or []):
        if not isinstance(vol, dict):
            errs.append(f"volumes[{i}] must be an object")
            continue
        kinds = set(vol) - {"name"}
        if not kinds or not kinds <= ALLOWED_VOLUME_TYPES:
            errs.append(f"volume {vol.get('name')!r}: only persistentVolumeClaim and emptyDir are allowed")
            continue
        if "persistentVolumeClaim" in vol:
            claim = (vol["persistentVolumeClaim"] or {}).get("claimName", "")
            if claim not in settings.allowed_pvcs:
                errs.append(f"volume {vol.get('name')!r}: PVC {claim!r} is not allowed")

    containers = pod.get("containers") or []
    if not containers:
        errs.append("pod spec needs at least one container")
    for i, c in enumerate(containers):
        errs.extend(_violations_container(c, i, settings, log))

    psc = pod.get("securityContext") or {}
    if psc.get("runAsUser") in (None, 0):
        errs.append("pod securityContext.runAsUser must be set to a non-root UID")

    try:
        pod["imagePullSecrets"] = _pull_secrets(pod.get("imagePullSecrets"), settings)
    except v.ValidationError as e:
        errs.append(e.message)

    deadline = spec.get("activeDeadlineSeconds")
    if not isinstance(deadline, int) or deadline <= 0 or deadline > settings.max_job_deadline_sec:
        errs.append(f"spec.activeDeadlineSeconds must be between 1 and {settings.max_job_deadline_sec}")

    if errs:
        return None, errs

    _force_safe_settings(job, pod)
    return job, []


def _pull_secrets(value, settings):
    requested = [s.get("name") for s in (value or []) if isinstance(s, dict)]
    names = list(settings.image_pull_secrets) + [n for n in v.pull_secrets(requested, settings) if n not in settings.image_pull_secrets]
    return [{"name": n} for n in names]


def _force_safe_settings(job, pod):
    spec = job["spec"]
    spec.setdefault("backoffLimit", 0)
    if not isinstance(spec.get("ttlSecondsAfterFinished"), int):
        spec["ttlSecondsAfterFinished"] = 3600

    labels = {"app.kubernetes.io/managed-by": "openvre", "openvre-job-id": job["metadata"]["name"]}
    job["metadata"].setdefault("labels", {}).update(labels)
    # The batch NetworkPolicy selects openvre.io/batch, so every Job pod must carry it.
    tmpl_meta = spec["template"].setdefault("metadata", {})
    tmpl_meta.setdefault("labels", {}).update({**labels, "openvre.io/batch": "true"})
    tmpl_meta.pop("annotations", None)

    pod["automountServiceAccountToken"] = False
    pod["enableServiceLinks"] = False
    pod.setdefault("restartPolicy", "Never")
    psc = pod.setdefault("securityContext", {})
    psc["runAsNonRoot"] = True
    psc.setdefault("seccompProfile", {"type": "RuntimeDefault"})
    if not pod.get("imagePullSecrets"):
        pod.pop("imagePullSecrets", None)
    for c in pod["containers"]:
        sc = c.setdefault("securityContext", {})
        sc["allowPrivilegeEscalation"] = False
        sc["privileged"] = False
        sc["capabilities"] = {"drop": ["ALL"]}


# Interactive sessions ----------------------------------------------------------
#
# Session templates can describe new tools without code changes, so this policy
# is what keeps any template, including a badly written one, inside safe limits.

SESSION_FORBIDDEN_POD_FIELDS = (
    "hostNetwork", "hostPID", "hostIPC", "hostUsers", "nodeName", "serviceAccountName",
    "serviceAccount", "ephemeralContainers", "shareProcessNamespace", "hostAliases",
    "runtimeClassName", "priorityClassName", "overhead",
)
SESSION_VOLUME_TYPES = {"persistentVolumeClaim", "emptyDir", "configMap"}


def _session_container(c, where, settings, admin_volumes, shared_admin_volumes, audit_rel, log):
    errs = []
    if not isinstance(c, dict):
        return [f"{where} must be an object"]
    is_audit = c.get("name") == "audit-sidecar" and c.get("image") == settings.audit_image and settings.audit_enabled
    if not is_audit:
        try:
            v.image(c.get("image"), settings, log)
        except v.ValidationError as e:
            errs.append(f"{where}: {e.message}")
    for f in FORBIDDEN_CONTAINER_FIELDS:
        if f in c:
            errs.append(f"{where}.{f} is not allowed")
    for i, env in enumerate(c.get("env") or []):
        if isinstance(env, dict) and "valueFrom" in env:
            src = env["valueFrom"] or {}
            if "secretKeyRef" in src or "configMapKeyRef" in src:
                errs.append(f"{where}.env[{i}] must not read Secrets or ConfigMaps")
    sc = c.get("securityContext") or {}
    if sc.get("privileged"):
        errs.append(f"{where}: privileged is not allowed")
    if (sc.get("capabilities") or {}).get("add"):
        errs.append(f"{where}: adding capabilities is not allowed")
    for p in c.get("ports") or []:
        if isinstance(p, dict) and (p.get("hostPort") or p.get("hostIP")):
            errs.append(f"{where}: hostPort is not allowed")
    for i, m in enumerate(c.get("volumeMounts") or []):
        if not isinstance(m, dict):
            continue
        if ".." in str(m.get("subPath", "")).split("/") or m.get("subPathExpr"):
            errs.append(f"{where}.volumeMounts[{i}]: subPath must not contain '..' or use subPathExpr")
        if is_audit:
            continue
        if m.get("name") in admin_volumes:
            errs.append(f"{where}: only the audit sidecar may mount the admin volume")
        elif m.get("name") in shared_admin_volumes:
            # One PVC holds both user data and the audit tree: the tool may only mount
            # a sub-folder of it, and never the audit folder.
            sub = str(m.get("subPath", "")).strip("/")
            if not sub or (audit_rel and (sub == audit_rel or sub.startswith(audit_rel + "/"))):
                errs.append(f"{where}.volumeMounts[{i}]: must use a subPath outside the audit folder")
    return errs


def check_session_objects(objects, spec, blocks, settings, log=None):
    """Check every object a session template produced and force safe settings.
    Returns (objects, errors)."""
    from . import manifests as mf

    objs = copy.deepcopy(objects)
    errs = []
    name = spec.name
    admin_pvc = settings.admindata_pvc or settings.userdata_pvc
    separate_admin = admin_pvc not in settings.allowed_pvcs

    for i, obj in enumerate(objs):
        kind = obj.get("kind")
        where = f"{kind} {obj.get('metadata', {}).get('name', '?')}"
        if kind not in mf.API_PATHS:
            errs.append(f"objects[{i}]: kind {kind!r} is not allowed")
            continue
        meta = obj.setdefault("metadata", {})
        oname = str(meta.get("name", ""))
        if not (oname == name or oname.startswith(name + "-")):
            errs.append(f"{where}: name must be the session name or start with it")
        try:
            v.dns_label(oname, f"{kind} name", 253)
        except v.ValidationError as e:
            errs.append(f"{where}: {e.message}")
        meta["namespace"] = spec.namespace
        for f in ("generateName", "ownerReferences", "finalizers"):
            meta.pop(f, None)
        meta["labels"] = {**(meta.get("labels") or {}), **blocks["labels"]}

        if kind == "Deployment":
            errs.extend(_check_session_deployment(obj, where, blocks, settings, admin_pvc, separate_admin, log))
        elif kind == "Service":
            sp = obj.setdefault("spec", {})
            if sp.get("type", "ClusterIP") != "ClusterIP" or sp.get("externalIPs") or sp.get("externalName"):
                errs.append(f"{where}: only ClusterIP Services are allowed")
            for p in sp.get("ports") or []:
                if isinstance(p, dict) and p.get("nodePort"):
                    errs.append(f"{where}: nodePort is not allowed")
            sp["selector"] = {"app": name}
            ann = {k: val for k, val in (meta.get("annotations") or {}).items()
                   if not k.startswith("openvre.io/gateway-")}
            if spec.access_type == "web" and oname == name:
                # The gateway routes the session path to this Service (scheduler /gateway/routes).
                ann.update(blocks["gateway_annotations"])
            # Desktop sessions are only reachable through Guacamole: never a gateway route.
            meta["annotations"] = ann
        elif kind == "ConfigMap":
            if obj.get("binaryData"):
                errs.append(f"{where}: binaryData is not allowed")
            data = obj.get("data") or {}
            if not all(isinstance(k, str) and isinstance(val, str) for k, val in data.items()):
                errs.append(f"{where}: data values must be strings")
    if spec.access_type == "web" and not any(
            o.get("kind") == "Service" and o.get("metadata", {}).get("name") == name for o in objs):
        errs.append(f"a web session needs a Service named {name!r}: the gateway routes to it")
    return (None if errs else objs), errs


def _check_session_deployment(obj, where, blocks, settings, admin_pvc, separate_admin, log):
    errs = []
    spec = obj.setdefault("spec", {})
    spec["replicas"] = 1
    spec["selector"] = {"matchLabels": {"app": blocks["name"]}}
    tmpl = spec.setdefault("template", {})
    tmeta = tmpl.setdefault("metadata", {})
    tmeta["labels"] = {**(tmeta.get("labels") or {}), **blocks["pod_labels"]}
    pod = tmpl.setdefault("spec", {})
    for f in SESSION_FORBIDDEN_POD_FIELDS:
        if f in pod and pod.get(f) not in (None, False, "", [], "default"):
            errs.append(f"{where}: pod spec field {f} is not allowed")
        pod.pop(f, None)

    admin_volume_names = set()
    shared_admin_volumes = set()
    for vol in pod.get("volumes") or []:
        if not isinstance(vol, dict):
            errs.append(f"{where}: volumes must be objects")
            continue
        kinds = set(vol) - {"name"}
        if not kinds or not kinds <= SESSION_VOLUME_TYPES:
            errs.append(f"{where}: volume {vol.get('name')!r}: only persistentVolumeClaim, emptyDir and configMap are allowed")
            continue
        if "persistentVolumeClaim" in vol:
            claim = (vol["persistentVolumeClaim"] or {}).get("claimName", "")
            if claim == admin_pvc and (separate_admin or vol.get("name") == "admindata"):
                if not settings.audit_enabled:
                    errs.append(f"{where}: the admin volume is only for the audit sidecar")
                admin_volume_names.add(vol.get("name"))
            elif claim not in settings.allowed_pvcs:
                errs.append(f"{where}: volume {vol.get('name')!r}: PVC {claim!r} is not allowed")
            elif claim == admin_pvc:
                shared_admin_volumes.add(vol.get("name"))
        if "configMap" in vol:
            cm = (vol["configMap"] or {}).get("name", "")
            if not str(cm).startswith(blocks["name"]):
                errs.append(f"{where}: volume {vol.get('name')!r}: only the session's own ConfigMaps can be mounted")

    prefix = settings.shared_data_prefix.rstrip("/")
    base = settings.audit_base.rstrip("/")
    audit_rel = base[len(prefix) + 1:] if base.startswith(prefix + "/") else ""
    args = (settings, admin_volume_names, shared_admin_volumes, audit_rel, log)
    containers = pod.get("containers") or []
    if not containers:
        errs.append(f"{where}: needs at least one container")
    for i, c in enumerate(containers):
        errs.extend(_session_container(c, f"{where}: containers[{i}]", *args))
    for i, c in enumerate(pod.get("initContainers") or []):
        errs.extend(_session_container(c, f"{where}: initContainers[{i}]", *args))

    pod["automountServiceAccountToken"] = False
    pod["enableServiceLinks"] = False
    if not pod.get("imagePullSecrets"):
        pod.pop("imagePullSecrets", None)
    else:
        try:
            names = [s.get("name") for s in pod["imagePullSecrets"] if isinstance(s, dict)]
            extra = [n for n in names if n not in settings.image_pull_secrets]
            v.pull_secrets(extra, settings)
        except v.ValidationError as e:
            errs.append(f"{where}: {e.message}")
    return errs
