"""Session routes for the openvre-gateway (Traefik).

Traefik's HTTP provider polls GET /gateway/routes every few seconds and uses the
answer as its dynamic configuration. The answer is built from the session
Services in each allowed namespace: their gateway annotations (path, request
headers, set by manifests.gateway_annotations) and their first port. Nothing is
kept in memory, so a restart of the scheduler or of Traefik loses no route.

Fixed routes (frontend, Keycloak, Guacamole) are not here: Traefik reads them
from its own file configuration (Helm chart).
"""

import json
import logging
import threading
import time

from . import manifests as mf
from .k8s import q

log = logging.getLogger("openvre-scheduler")

# Above the fixed routes ("/" has Traefik's default, length-based priority).
ROUTER_PRIORITY = 1000
CACHE_SEC = 2.0

_cache = {"at": 0.0, "config": None}
_lock = threading.Lock()


def _session_route(svc, settings):
    """(name, router, middlewares, service) for one session Service, or None."""
    meta = svc.get("metadata") or {}
    ann = meta.get("annotations") or {}
    name = meta.get("name", "")
    ns = meta.get("namespace", "")
    path = ann.get(mf.GATEWAY_PATH_ANNOTATION, "")
    ports = (svc.get("spec") or {}).get("ports") or []
    if not (name and ns and path.startswith("/interactive-tool/") and ports):
        return None
    port = ports[0].get("port")
    try:
        headers = json.loads(ann.get(mf.GATEWAY_HEADERS_ANNOTATION) or "{}")
    except ValueError:
        log.warning("bad gateway headers annotation", extra={"service": name})
        headers = {}

    middlewares = {}
    chain = []
    if settings.auth_url:
        auth = {"address": settings.auth_url, "trustForwardHeader": True}
        middlewares[f"{name}-auth"] = {"forwardAuth": auth}
        chain.append(f"{name}-auth")
    # The tool sees "/" instead of the session path; Traefik also sends X-Forwarded-Prefix.
    middlewares[f"{name}-strip"] = {"stripPrefix": {"prefixes": [path]}}
    chain.append(f"{name}-strip")
    if headers:
        middlewares[f"{name}-headers"] = {"headers": {"customRequestHeaders": {str(k): str(v) for k, v in headers.items()}}}
        chain.append(f"{name}-headers")

    router = {
        "rule": f"PathPrefix(`{path}`)",
        "priority": ROUTER_PRIORITY,
        "entryPoints": ["web"],
        "middlewares": chain,
        "service": name,
    }
    service = {"loadBalancer": {"servers": [{"url": f"http://{name}.{ns}.svc:{port}"}], "passHostHeader": True}}
    return name, router, middlewares, service


def build(kube, settings):
    """Traefik dynamic configuration (dict) for every live session."""
    routers, middlewares, services = {}, {}, {}
    for ns in settings.allowed_namespaces:
        for svc in kube.list(f"/api/v1/namespaces/{q(ns)}/services", f"{mf.INTERACTIVE_LABEL}=true"):
            labels = (svc.get("metadata") or {}).get("labels") or {}
            if labels.get("app.kubernetes.io/managed-by") != mf.MANAGED_BY:
                continue
            route = _session_route(svc, settings)
            if route is None:
                continue
            name, router, mws, service = route
            routers[name] = router
            middlewares.update(mws)
            services[name] = service
    http = {}
    # Traefik refuses empty sections, so leave them out.
    if routers:
        http = {"routers": routers, "middlewares": middlewares, "services": services}
    return {"http": http} if http else {}


def routes(kube, settings):
    """build(), cached for a couple of seconds: Traefik polls often."""
    with _lock:
        if _cache["config"] is None or time.monotonic() - _cache["at"] > CACHE_SEC:
            _cache["config"] = build(kube, settings)
            _cache["at"] = time.monotonic()
        return _cache["config"]


def clear_cache():
    with _lock:
        _cache["config"] = None
