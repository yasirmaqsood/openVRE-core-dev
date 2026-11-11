"""HTTP API. Standard library only: ThreadingHTTPServer, one thread per request.

Routes (each also available under /v1/...):
  GET    /healthz                      liveness, no auth
  GET    /readyz                       readiness: Kubernetes API reachable, no auth
  GET    /templates                    list workload templates and their parameters
  GET    /templates/<name>             one template
  POST   /jobs                         create a batch Job: {"template", "params"} (policy checked)
  GET    /jobs/<name>?namespace=       Job status
  DELETE /jobs/<name>?namespace=       delete a Job
  POST   /interactive-sessions         create a session: {"template", "context", "params"}
  GET    /interactive-sessions/<name>  session status
  DELETE /interactive-sessions/<name>  delete a session
  GET    /gateway/routes               Traefik dynamic configuration of the session routes;
                                       only with X-OpenVRE-Gateway-Token (the gateway's token)
"""

import hmac
import json
import logging
import signal
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, config, gateway, jobs, sessions, templates
from . import validation as v
from .auth import AuthError, Authenticator, may_access
from .k8s import K8sError, KubeClient

log = logging.getLogger("openvre_scheduler")

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, val in record.__dict__.items():
            if k not in _STD_ATTRS and not k.startswith("_"):
                out[k] = val
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level="INFO"):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))


def k8s_error_status(e):
    """Map a Kubernetes API error to the status this API returns."""
    if e.status == 0:
        return 503
    if e.status in (409, 422):
        return e.status
    return 502


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = f"openvre-scheduler/{__version__}"
    sys_version = ""
    # HTTP/1.0: one request per connection. Simple, and an early error reply
    # (e.g. 401 before the body is read) cannot confuse a kept-alive connection.
    protocol_version = "HTTP/1.0"
    settings = None
    kube = None
    templates = {}
    auth = None
    _ready_cache = {"at": 0.0, "ok": False, "error": ""}
    _ready_lock = threading.Lock()

    def setup(self):
        self.timeout = self.settings.request_timeout_sec
        super().setup()

    # Plumbing ------------------------------------------------------------

    def log_message(self, fmt, *args):
        # Access logs are written in _finish as JSON instead.
        return

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self.request_id)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        self._status = status

    def _error(self, status, message):
        self._send(status, {"ok": False, "error": message, "request_id": self.request_id})

    def _body(self):
        length = self.headers.get("Content-Length")
        if length is None:
            raise ApiError(411, "Content-Length is required")
        try:
            length = int(length)
        except ValueError:
            raise ApiError(400, "invalid Content-Length")
        if length > self.settings.max_body_bytes:
            raise ApiError(413, f"request body larger than {self.settings.max_body_bytes} bytes")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "request body is not valid JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "request body must be a JSON object")
        return body

    def _route(self):
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        if path.startswith("/v1/"):
            path = path[3:]
        query = urllib.parse.parse_qs(parts.query)
        segs = [urllib.parse.unquote(s) for s in path.strip("/").split("/") if s]
        return path, segs, query

    def _handle(self, method):
        self.request_id = (self.headers.get("X-Request-ID") or "")[:64] or uuid.uuid4().hex
        self._status = 0
        self.caller = None
        self.user = None
        start = time.monotonic()
        path, segs, query = self._route()
        try:
            if path == "/healthz" and method == "GET":
                return self._send(200, {"ok": True, "version": __version__})
            if path == "/readyz" and method == "GET":
                return self._readyz()
            if path == "/gateway/routes" and method == "GET":
                return self._gateway_routes()
            try:
                self.caller = self.auth.caller(self.headers.get("Authorization", ""))
                if segs[:1] in (["jobs"], ["interactive-sessions"]):
                    self.user = self.auth.user(self.headers.get("X-OpenVRE-User-Token"))
            except AuthError as e:
                raise ApiError(e.status, e.message)
            ns_param = (query.get("namespace") or [""])[0]
            if segs[:1] == ["jobs"]:
                return self._jobs(method, segs, ns_param)
            if segs[:1] == ["interactive-sessions"]:
                return self._sessions(method, segs, ns_param)
            if segs[:1] == ["templates"] and method == "GET":
                return self._templates(segs)
            raise ApiError(404, "not found")
        except ApiError as e:
            self._error(e.status, e.message)
        except v.ValidationError as e:
            self._error(e.status, e.message)
        except K8sError as e:
            status = k8s_error_status(e)
            log.error("kubernetes API error", extra={"request_id": self.request_id, "k8s_status": e.status,
                                                    "reason": e.reason, "error": e.message})
            self._error(status, f"Kubernetes API error: {e.message}")
        except Exception:  # noqa: BLE001 - never leak a traceback to the client
            log.exception("unhandled error", extra={"request_id": self.request_id})
            self._error(500, "internal error")
        finally:
            # The gateway polls every few seconds: keep those out of the INFO log.
            level = logging.DEBUG if path == "/gateway/routes" and self._status == 200 else logging.INFO
            log.log(level, "request", extra={
                "request_id": self.request_id, "method": method, "path": path,
                "status": self._status, "duration_ms": round((time.monotonic() - start) * 1000, 1),
                "client": self.client_address[0] if self.client_address else "",
                "caller": self.caller.name if self.caller else "",
                "user": self.user.log_name() if self.user else "",
            })

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    # Endpoints -----------------------------------------------------------

    def _gateway_routes(self):
        token = (self.headers.get("X-OpenVRE-Gateway-Token") or "").strip().encode("utf-8")
        if not token or not hmac.compare_digest(token, self.settings.gateway_token.encode("utf-8")):
            raise ApiError(401, "gateway token missing or invalid")
        return self._send(200, gateway.routes(self.kube, self.settings))

    def _readyz(self):
        cache = Handler._ready_cache
        with Handler._ready_lock:
            if time.monotonic() - cache["at"] > 10:
                try:
                    self.kube.ping()
                    cache.update(ok=True, error="")
                except K8sError as e:
                    cache.update(ok=False, error=e.message)
                cache["at"] = time.monotonic()
        if cache["ok"]:
            return self._send(200, {"ok": True})
        return self._send(503, {"ok": False, "error": cache["error"]})

    def _jobs(self, method, segs, ns_param):
        s, kube = self.settings, self.kube
        if method == "POST" and len(segs) == 1:
            body = self._body()
            ns = v.namespace(body.get("namespace"), s)
            job, template = jobs.create(kube, s, self.templates, ns, body, owner=self.user.owner if self.user else None)
            name = job.get("metadata", {}).get("name", "")
            log.info("job created", extra={"request_id": self.request_id, "job": name, "namespace": ns,
                                           "template": template or "(raw manifest)"})
            return self._send(201, {"ok": True, "name": name, "template": template,
                                    "stdout": f"job.batch/{name} created", "stderr": ""})
        if len(segs) != 2:
            raise ApiError(404, "not found")
        ns = v.namespace(ns_param, s)
        name = v.job_name(segs[1])
        if method == "GET":
            job = jobs.get(kube, ns, name)
            if job is not None and not may_access(self.user, job, s):
                job = None  # someone else's Job: answer as if it did not exist
            if job is None:
                return self._send(200, {"ok": True, "exists": False, "job": ""})
            return self._send(200, {"ok": True, "exists": True, "status": jobs.summarize(job),
                                    # Raw Job kept as a string for frontends that parse it themselves.
                                    "job": json.dumps(job)})
        if method == "DELETE":
            job = jobs.get(kube, ns, name)
            if job is not None and not may_access(self.user, job, s):
                raise ApiError(404, "not found")
            deleted = jobs.delete(kube, ns, name)
            msg = f"job.batch/{name} deleted" if deleted else f"job.batch/{name} already deleted"
            log.info("job deleted", extra={"request_id": self.request_id, "job": name, "found": deleted})
            return self._send(200, {"ok": True, "stdout": msg, "stderr": ""})
        raise ApiError(405, "method not allowed")

    def _templates(self, segs):
        if len(segs) == 1:
            return self._send(200, {"ok": True, "templates": [t.describe() for t in self.templates.values()]})
        if len(segs) == 2 and segs[1] in self.templates:
            return self._send(200, {"ok": True, "template": self.templates[segs[1]].describe()})
        raise ApiError(404, "unknown template")

    def _sessions(self, method, segs, ns_param):
        s, kube = self.settings, self.kube
        if method == "POST" and len(segs) == 1:
            tpl, spec, values = sessions.parse_request(self._body(), s, self.templates)
            if self.user:
                spec.owner, spec.owner_sub, spec.owner_name = self.user.owner, self.user.sub, self.user.username
                spec.owner_email = self.user.email
            sessions.create(kube, s, tpl, spec, values)
            gateway.clear_cache()
            log.info("session created", extra={"request_id": self.request_id, "session": spec.name,
                                               "namespace": spec.namespace, "user": spec.user_id,
                                               "template": spec.template, "image": spec.image})
            return self._send(201, {
                "ok": True,
                "name": spec.name,
                "session_id": spec.session_id,
                "template": spec.template,
                # How to reach the session: {"type": "web", "url"} or
                # {"type": "guacamole", "host", "port", "protocol"}.
                "access": spec.access,
                "access_url": spec.access.get("url", ""),
                "path_prefix": spec.path_prefix,
                "audit_host_path": spec.audit_host_path,
                "expires_at": spec.expires_at,
            })
        if len(segs) != 2:
            raise ApiError(404, "not found")
        ns = v.namespace(ns_param, s)
        name = v.session_name(segs[1])
        if method == "GET":
            st = sessions.status(kube, ns, name)
            if st is not None and not may_access(self.user, st["deployment"], s):
                st = None
            if st is None:
                return self._send(200, {"ok": True, "exists": False, "deployment": ""})
            out = {"ok": True, "exists": True, "state": st["state"], "reason": st.get("reason", ""),
                   "deployment": json.dumps(st["deployment"])}
            if st.get("message"):
                out["message"] = st["message"]
            return self._send(200, out)
        if method == "DELETE":
            dep = sessions.get_deployment(kube, ns, name)
            if dep is not None and not may_access(self.user, dep, s):
                raise ApiError(404, "not found")
            found, errors = sessions.delete(kube, s, ns, name)
            gateway.clear_cache()
            if errors:
                raise ApiError(502, "could not delete every session object: " + "; ".join(errors))
            log.info("session deleted", extra={"request_id": self.request_id, "session": name, "found": found})
            return self._send(200, {"ok": True, "stdout": f"deleted {name}" if found else f"{name} already deleted",
                                    "stderr": ""})
        raise ApiError(405, "method not allowed")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(settings, kube, tpls):
    handler = type("BoundHandler", (Handler,), {"settings": settings, "kube": kube, "templates": tpls,
                                                "auth": Authenticator(settings, kube)})
    return Server((settings.listen_host, settings.listen_port), handler)


def main():
    setup_logging()
    try:
        settings = config.load()
    except config.ConfigError as e:
        log.critical("invalid configuration", extra={"error": str(e)})
        sys.exit(2)
    setup_logging(settings.log_level)
    if config.is_placeholder_token(settings):
        log.warning("SCHEDULER_AUTH_TOKEN still starts with CHANGEME; set a real secret")
    try:
        tpls = templates.load_dir(settings.template_dir)
    except templates.TemplateError as e:
        log.critical("invalid template", extra={"error": str(e), "dir": settings.template_dir})
        sys.exit(2)

    kube = KubeClient(settings)
    server = make_server(settings, kube, tpls)
    stop = threading.Event()
    sessions.start_reaper(kube, settings, stop)

    def shutdown(signum, _frame):
        log.info("shutting down", extra={"signal": signum})
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info("scheduler started", extra={
        "version": __version__, "port": settings.listen_port, "namespaces": settings.allowed_namespaces,
        "strict": settings.strict, "audit": settings.audit_enabled,
        "session_ttl_hours": settings.session_ttl_hours, "max_sessions_per_user": settings.max_sessions_per_user,
        "templates": sorted(tpls), "raw_job_manifests": settings.allow_raw_job_manifests,
        "user_token": settings.user_token,
    })
    server.serve_forever()
    server.server_close()
