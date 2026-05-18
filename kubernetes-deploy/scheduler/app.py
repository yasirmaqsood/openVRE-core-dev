import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer


TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
API_HOST = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
API_PORT = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
DEFAULT_NAMESPACE = os.environ.get("DEFAULT_NAMESPACE", "default")
SCHEDULER_AUTH_TOKEN = os.environ.get("SCHEDULER_AUTH_TOKEN", "")
OPENVRE_INTERACTIVE_AUTH_URL = os.environ.get("OPENVRE_INTERACTIVE_AUTH_URL", "")
OPENVRE_INTERACTIVE_AUTH_SIGNIN = os.environ.get("OPENVRE_INTERACTIVE_AUTH_SIGNIN", "")
INTERACTIVE_LABEL = "openvre.io/interactive"
INTERACTIVE_MANAGED_BY = "openvre-scheduler"

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


def sanitize_k8s_name(value, max_len=50):
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = value.strip("-")
    if not value:
        value = "openvre-ix-session"
    if len(value) > max_len:
        value = value[:max_len].rstrip("-")
    return value


def build_rserver_conf(auth_none=False):
    # Drop-in only: do not replace the image default rserver.conf (breaks auth).
    conf = "rsession-which-r=/usr/local/bin/R\n"
    if auth_none:
        conf += "auth-none=1\n"
    return conf


def deployment_manifest(
    name,
    namespace,
    image,
    path_prefix,
    pvc,
    cpu,
    memory,
    password,
    uid,
    gid,
    use_openvre_auth=False,
):
    container_env = [
        {"name": "PASSWORD", "value": password},
        {"name": "DISABLE_SECURE_COOKIES", "value": "true"},
    ]
    if use_openvre_auth:
        container_env.append({"name": "DISABLE_AUTH", "value": "true"})

    volume_mounts = [
        {"name": "shared", "mountPath": "/home/rstudio/workspace"},
    ]
    volumes = [
        {"name": "shared", "persistentVolumeClaim": {"claimName": pvc}},
    ]
    if not use_openvre_auth:
        volume_mounts.append(
            {
                "name": "rserver-conf",
                "mountPath": "/etc/rstudio/rserver.conf.d/99-openvre.conf",
                "subPath": "openvre.conf",
            }
        )
        volumes.append(
            {"name": "rserver-conf", "configMap": {"name": f"{name}-conf"}}
        )

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
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {
                    "labels": {
                        "app": name,
                        INTERACTIVE_LABEL: "true",
                    }
                },
                "spec": {
                    "securityContext": {"fsGroup": max(1, int(gid))},
                    "containers": [
                        {
                            "name": "interactive",
                            "image": image,
                            "ports": [{"containerPort": 8787, "name": "http"}],
                            "env": container_env,
                            "resources": {
                                "requests": {"cpu": cpu, "memory": memory},
                                "limits": {"cpu": cpu, "memory": memory},
                            },
                            "volumeMounts": volume_mounts,
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }


def service_manifest(name, namespace):
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
            "ports": [{"name": "http", "port": 8787, "targetPort": 8787}],
        },
    }


def ingress_manifest(name, namespace, path_prefix, service_name, external_base):
    path = path_prefix if path_prefix.startswith("/") else f"/{path_prefix}"
    path = path.rstrip("/")
    regex_path = f"{path}(/|$)(.*)"
    base = (external_base or "").rstrip("/")
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
            "ingressClassName": "nginx",
            "rules": [
                {
                    "http": {
                        "paths": [
                            {
                                "path": regex_path,
                                "pathType": "ImplementationSpecific",
                                "backend": {
                                    "service": {
                                        "name": service_name,
                                        "port": {"number": 8787},
                                    }
                                },
                            }
                        ]
                    }
                }
            ],
        },
    }


def configmap_manifest(name, namespace, auth_none=False):
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{name}-conf",
            "namespace": namespace,
            "labels": {INTERACTIVE_LABEL: "true"},
        },
        "data": {"openvre.conf": build_rserver_conf(auth_none=auth_none)},
    }


def proxy_headers_manifest(name, namespace, path_prefix):
    path = path_prefix if path_prefix.startswith("/") else f"/{path_prefix}"
    path = path.rstrip("/")
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{name}-proxy-hdr",
            "namespace": namespace,
            "labels": {INTERACTIVE_LABEL: "true"},
        },
        "data": {
            "X-RStudio-Root-Path": path,
            "X-Forwarded-Proto": "http",
        },
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
                image = body.get("image", "rocker/rstudio:4.4.2")
                pvc = body.get("pvc", "dashboard-frontend-sgecore-shareddata")
                password = body.get("password", "openvre")
                cpu = body.get("cpu", "500m")
                memory = body.get("memory", "1536Mi")
                run_as_uid = int(body.get("run_as_uid", 1000))
                run_as_gid = int(body.get("run_as_gid", 1000))
                external_base = (body.get("external_base") or "").rstrip("/")

                if not user_id:
                    return self._json(400, {"ok": False, "error": "user_id is required"})
                if not session_id:
                    session_id = "rstudio-" + os.urandom(4).hex()

                user_seg = sanitize_path_segment(user_id)
                session_seg = sanitize_path_segment(session_id, 32)
                path_prefix = f"/{user_seg}/{session_seg}"
                name = sanitize_k8s_name(f"openvre-ix-{session_seg}")

                use_openvre_auth = bool(OPENVRE_INTERACTIVE_AUTH_URL)
                manifests = [
                    proxy_headers_manifest(name, ns, path_prefix),
                    deployment_manifest(
                        name,
                        ns,
                        image,
                        path_prefix,
                        pvc,
                        cpu,
                        memory,
                        password,
                        run_as_uid,
                        run_as_gid,
                        use_openvre_auth=use_openvre_auth,
                    ),
                ]
                if not use_openvre_auth:
                    manifests.insert(0, configmap_manifest(name, ns, auth_none=False))
                manifests.extend([
                    service_manifest(name, ns),
                    ingress_manifest(name, ns, path_prefix, name, external_base),
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
                        self._delete_interactive(name, ns)
                        return self._json(
                            500,
                            {
                                "ok": False,
                                "error": f"failed to create {kind}: {raw}",
                            },
                        )

                access_url = path_prefix + "/"
                return self._json(
                    200,
                    {
                        "ok": True,
                        "name": name,
                        "session_id": session_seg,
                        "user_path": user_seg,
                        "access_url": access_url,
                        "path_prefix": path_prefix,
                    },
                )
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        return self._json(404, {"ok": False, "error": "not found"})

    def _delete_interactive(self, name, ns):
        for kind, path in [
            ("Ingress", f"/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses/{name}"),
            ("Service", f"/api/v1/namespaces/{ns}/services/{name}"),
            ("Deployment", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"),
            ("ConfigMap", f"/api/v1/namespaces/{ns}/configmaps/{name}-conf"),
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
            self._delete_interactive(name, ns)
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
