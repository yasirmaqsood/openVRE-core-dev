"""Batch Jobs: create from a template (or, if allowed, a raw manifest), read status, delete."""

import json
import logging

from . import policy
from . import validation as v
from .auth import OWNER_LABEL
from .k8s import q

log = logging.getLogger("openvre_scheduler.jobs")

MANAGED_LABEL = ("app.kubernetes.io/managed-by", "openvre")


def _path(ns, name=None):
    base = f"/apis/batch/v1/namespaces/{q(ns)}/jobs"
    return base if name is None else f"{base}/{q(name)}"


def parse_manifest(raw):
    """A raw Job manifest as a JSON object (older frontends: a JSON string)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except ValueError:
            raise v.ValidationError("manifest must be JSON (YAML is not accepted)")
        if isinstance(obj, dict):
            return obj
    raise v.ValidationError("manifest must be a JSON object")


def build(body, ns, settings, templates):
    """Turn a POST /jobs body into a checked Job. Returns (job, template name or "")."""
    if "template" in body:
        name = v.plain_text(body.get("template"), "template", 63, required=True)
        tpl = templates.get(name)
        v.require(tpl is not None, f"unknown template {name!r}", 404)
        v.require(tpl.kind == "Job", f"template {name!r} does not create a Job")
        manifest, _ = tpl.render(body.get("params") or {}, settings)
        source = name
    elif "manifest" in body:
        v.require(settings.allow_raw_job_manifests,
                  "raw manifests are disabled; send {\"template\": ..., \"params\": {...}}", 400)
        manifest = parse_manifest(body["manifest"])
        source = ""
    else:
        raise v.ValidationError("send {\"template\": ..., \"params\": {...}}")

    # The policy runs on templated Jobs too: a mistake in a template file must
    # not be able to produce a privileged or secret-reading pod.
    job, errors = policy.check_job(manifest, ns, settings, log)
    if errors:
        raise v.ValidationError("job rejected by policy: " + "; ".join(errors), 422)
    return job, source


def create(kube, settings, templates, ns, body, owner=None):
    job, source = build(body, ns, settings, templates)
    if owner:
        # Verified user (layer 2): only this user may read or delete the Job.
        job["metadata"].setdefault("labels", {})[OWNER_LABEL] = owner
        job["spec"]["template"].setdefault("metadata", {}).setdefault("labels", {})[OWNER_LABEL] = owner
    created = kube.create(_path(ns), job)
    return (created or job), source


def _is_managed(job):
    labels = (job or {}).get("metadata", {}).get("labels") or {}
    return labels.get(MANAGED_LABEL[0]) == MANAGED_LABEL[1]


def summarize(job):
    """A plain state the frontend can use without parsing the whole Job."""
    st = job.get("status") or {}
    conditions = {c.get("type"): c for c in st.get("conditions") or [] if c.get("status") == "True"}
    if "Complete" in conditions or st.get("succeeded"):
        return {"state": "Succeeded", "reason": ""}
    if "Failed" in conditions or st.get("failed"):
        c = conditions.get("Failed") or {}
        return {"state": "Failed", "reason": c.get("reason", ""), "message": c.get("message", "")}
    if st.get("active"):
        return {"state": "Running", "reason": ""}
    return {"state": "Pending", "reason": ""}


def get(kube, ns, name):
    job = kube.get(_path(ns, name))
    if job is None or not _is_managed(job):
        return None
    return job


def delete(kube, ns, name):
    """Returns False if the Job was already gone. Unmanaged Jobs count as not found."""
    job = kube.get(_path(ns, name))
    if job is None or not _is_managed(job):
        return False
    return kube.delete(_path(ns, name), body={"apiVersion": "batch/v1", "kind": "DeleteOptions",
                                              "propagationPolicy": "Background"})
