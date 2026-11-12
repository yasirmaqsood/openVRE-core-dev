"""Shared test helpers: settings for tests and an in-memory stand-in for the Kubernetes API."""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openvre_scheduler import config  # noqa: E402
from openvre_scheduler.k8s import K8sError  # noqa: E402

TOKEN = "test-token-0123456789abcdef"

BASE_ENV = {
    "SCHEDULER_AUTH_TOKEN": TOKEN,
    "DEFAULT_NAMESPACE": "openvre",
    "DEFAULT_SHARED_PVC": "shared-pvc",
    "DEFAULT_USERDATA_PVC": "shared-pvc",
    "DEFAULT_ADMINDATA_PVC": "admin-pvc",
    "OPENVRE_INGRESS_CLASS": "nginx",
    "OPENVRE_EXTERNAL_BASE": "https://vre.example.org",
    "OPENVRE_INTERACTIVE_DEFAULT_PORT": "8787",
    "OPENVRE_INTERACTIVE_DEFAULT_MOUNT": "/workspace",
}


def settings(**overrides):
    env = dict(BASE_ENV)
    env.update({k: str(val) for k, val in overrides.items()})
    return config.load(env)


class FakeKube:
    """Stores objects by API path. Behaves like KubeClient for the calls the scheduler makes."""

    def __init__(self):
        self.objects = {}
        self.fail_create_kind = None
        self.unreachable = False
        self.forbidden = ()  # path parts the "Role" does not allow, e.g. ("/ingresses",)
        self.created = []
        self.deleted = []
        self.logs = {}  # "pod/container" -> text

    def _check(self, path=""):
        if self.unreachable:
            raise K8sError(0, "Kubernetes API unreachable: test")
        if any(part in path for part in self.forbidden):
            raise K8sError(403, f"forbidden: {path}", "Forbidden")

    def get(self, path, query=None, accept="application/json"):
        self._check(path)
        obj = self.objects.get(path)
        return copy.deepcopy(obj) if obj is not None else None

    def create(self, path, obj):
        self._check(path)
        if self.fail_create_kind and obj.get("kind") == self.fail_create_kind:
            raise K8sError(422, f"{obj['kind']} is invalid", "Invalid")
        full = f"{path}/{obj['metadata']['name']}"
        if full in self.objects:
            raise K8sError(409, "already exists", "AlreadyExists")
        self.objects[full] = copy.deepcopy(obj)
        self.created.append(full)
        return copy.deepcopy(obj)

    def delete(self, path, body=None):
        self._check(path)
        self.deleted.append(path)
        return self.objects.pop(path, None) is not None

    def list(self, path, label_selector=None):
        self._check(path)
        wanted = {}
        for part in (label_selector or "").split(","):
            if "=" in part:
                k, val = part.split("=", 1)
                wanted[k] = val
        out = []
        for p, obj in self.objects.items():
            if p.rsplit("/", 1)[0] != path:
                continue
            labels = obj.get("metadata", {}).get("labels") or {}
            if all(labels.get(k) == val for k, val in wanted.items()):
                out.append(copy.deepcopy(obj))
        return out

    def ping(self):
        self._check()

    def get_text(self, path, query=None):
        self._check()
        pod = path.rstrip("/").split("/")[-2]
        key = f"{pod}/{(query or {}).get('container')}"
        if (query or {}).get("previous"):
            key += "/previous"
        return self.logs.get(key, "")


def php_job_manifest(name="openvre-bj-user-run-1a2b3c4d", ns="openvre", image="ghcr.io/inab/tool:1.0"):
    """The Job manifest ProcessK8s.php builds, as JSON-decoded by Python."""
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": {"app.kubernetes.io/managed-by": "openvre", "openvre-job-id": name},
        },
        "spec": {
            "ttlSecondsAfterFinished": 120,
            "activeDeadlineSeconds": 86400,
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/managed-by": "openvre", "openvre-job-id": name,
                                        "openvre.io/batch": "true"}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "containers": [{
                        "name": "tool-runner",
                        "image": image,
                        "command": ["bash", "-lc", 'cd "$OPENVRE_WORKDIR" && bash "$OPENVRE_SUBMIT_SCRIPT"'],
                        "env": [{"name": "OPENVRE_WORKDIR", "value": "/shared_data/userdata/u/p/run1"},
                                {"name": "OPENVRE_SUBMIT_SCRIPT", "value": "/shared_data/userdata/u/p/run1/.submit"}],
                        "resources": {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "2Gi"}},
                        "securityContext": {"allowPrivilegeEscalation": False},
                        "volumeMounts": [{"name": "shared-data", "mountPath": "/shared_data/"}],
                    }],
                    "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000},
                    "volumes": [{"name": "shared-data", "persistentVolumeClaim": {"claimName": "shared-pvc"}}],
                    "tolerations": [{"key": "node.kubernetes.io/not-ready", "operator": "Exists",
                                     "effect": "NoExecute", "tolerationSeconds": 60}],
                },
            },
        },
    }


TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def load_templates():
    from openvre_scheduler import templates
    return templates.load_dir(TEMPLATE_DIR)


def session_body(template="openvre-web-session", context=None, params=None, **ctx_overrides):
    """A POST /interactive-sessions body as ProcessK8s.php sends it."""
    ctx = {
        "user_id": "alice",
        "session_id": "rstudio-abc12345",
        "tool_id": "rstudio",
        "tool_name": "RStudio",
        "external_base": "http://evil.example",
        "workspace_mount_path": "/home/rstudio",
        "working_dir": "/shared_data/userdata/alice/proj/run1",
        "project": "proj",
        "execution": "run1",
        "volume_mounts": [
            {"host_path": "/shared_data/userdata/alice/proj/repository", "container_path": "/home/rstudio/repository", "read_only": True},
            {"host_path": "/shared_data/userdata/alice/proj/rstudio_data", "container_path": "/home/rstudio"},
        ],
    }
    ctx.update(ctx_overrides)
    if context:
        ctx.update(context)
    if params is None:
        params = {"image": "ghcr.io/inab/k8-tool-rstudio:1.0", "port": 8787, "cpu": "1", "memory": "2Gi"}
    return {"namespace": "openvre", "template": template, "context": ctx, "params": params}


def desktop_body(**ctx_overrides):
    return session_body("openvre-desktop-session",
                        params={"image": "ghcr.io/inab/desktop:1.0", "port": 5901, "protocol": "vnc"}, **ctx_overrides)
