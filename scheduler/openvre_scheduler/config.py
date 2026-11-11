"""Settings read from environment variables once at startup.

Every variable the scheduler understands is listed in scheduler/README.md.
"""

import os
from dataclasses import dataclass, field

from . import validation


class ConfigError(Exception):
    pass


TRUE_VALUES = ("1", "true", "yes", "on")


def _str(env, name, default=""):
    return (env.get(name, default) or "").strip()


def _bool(env, name, default=False):
    raw = _str(env, name)
    if raw == "":
        return default
    return raw.lower() in TRUE_VALUES


def _int(env, name, default):
    raw = _str(env, name)
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")


def _list(env, name):
    return [p.strip() for p in _str(env, name).split(",") if p.strip()]


@dataclass
class Settings:
    # HTTP server
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    auth_tokens: list = field(default_factory=list)
    # Read-only token the gateway (Traefik) sends to GET /gateway/routes.
    gateway_token: str = ""
    # User check: "off", "optional" (verify when sent) or "required".
    user_token: str = "off"
    oidc_issuer: str = ""
    oidc_jwks_url: str = ""
    oidc_client_id: str = ""
    oidc_admin_roles: list = field(default_factory=list)
    max_body_bytes: int = 1024 * 1024
    request_timeout_sec: int = 30

    # Kubernetes API access
    api_host: str = "kubernetes.default.svc"
    api_port: str = "443"
    token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    k8s_timeout_sec: int = 30

    # Where the scheduler may act
    default_namespace: str = "default"
    allowed_namespaces: list = field(default_factory=list)

    # Storage
    shared_pvc: str = ""
    userdata_pvc: str = ""
    admindata_pvc: str = ""
    tools_pvc: str = ""
    allowed_pvcs: list = field(default_factory=list)
    shared_data_prefix: str = "/shared_data"

    # Images
    strict: bool = False
    image_allowlist: list = field(default_factory=list)
    allowed_registries: list = field(default_factory=list)
    image_rules: list = field(default_factory=list)
    image_pull_secrets: list = field(default_factory=list)
    allowed_pull_secrets: list = field(default_factory=list)

    # Interactive sessions
    external_base: str = ""
    auth_url: str = ""
    interactive_default_image: str = ""
    interactive_default_port: str = ""
    interactive_default_mount: str = ""
    node_selector: str = ""
    tolerations: str = ""
    preferred_hostname: str = ""
    eviction_toleration_seconds: int = 60
    session_ttl_hours: int = 0
    max_sessions_per_user: int = 0
    reaper_interval_sec: int = 300
    max_session_cpu: str = ""
    max_session_memory: str = ""

    # Batch jobs
    max_job_deadline_sec: int = 7 * 24 * 3600
    default_job_ttl_sec: int = 3600
    job_run_as_uid: int = 1000
    job_run_as_gid: int = 1000

    # Workload templates
    template_dir: str = ""
    allow_raw_job_manifests: bool = False

    # Audit
    audit_enabled: bool = False
    audit_image: str = ""
    audit_interval_sec: str = "5"
    audit_base: str = "/shared_data/admindata"
    audit_watch_sec: int = 900
    audit_log_tail_lines: int = 200

    # Logging
    log_level: str = "INFO"


def load(env=None):
    """Build Settings from env (defaults to os.environ) and check it.

    Raises ConfigError with every problem found, so a bad deployment fails at
    startup instead of on the first request.
    """
    env = os.environ if env is None else env
    s = Settings()

    # Not SCHEDULER_PORT: Kubernetes sets that to "tcp://..." for a Service named "scheduler".
    s.listen_port = _int(env, "SCHEDULER_LISTEN_PORT", 8080)
    # Several comma-separated tokens are accepted so a token can be rotated
    # without downtime: add the new one, roll the frontend, remove the old one.
    s.auth_tokens = _list(env, "SCHEDULER_AUTH_TOKEN")
    s.gateway_token = _str(env, "OPENVRE_GATEWAY_TOKEN")
    s.user_token = (_str(env, "OPENVRE_USER_TOKEN", "off") or "off").lower()
    s.oidc_issuer = _str(env, "OPENVRE_OIDC_ISSUER").rstrip("/")
    s.oidc_jwks_url = _str(env, "OPENVRE_OIDC_JWKS_URL") or (s.oidc_issuer + "/protocol/openid-connect/certs" if s.oidc_issuer else "")
    s.oidc_client_id = _str(env, "OPENVRE_OIDC_CLIENT_ID")
    s.oidc_admin_roles = _list(env, "OPENVRE_OIDC_ADMIN_ROLES")
    s.max_body_bytes = _int(env, "SCHEDULER_MAX_BODY_BYTES", s.max_body_bytes)
    s.request_timeout_sec = _int(env, "SCHEDULER_REQUEST_TIMEOUT_SEC", s.request_timeout_sec)

    s.api_host = _str(env, "KUBERNETES_SERVICE_HOST", s.api_host)
    s.api_port = _str(env, "KUBERNETES_SERVICE_PORT", s.api_port)
    s.token_path = _str(env, "SCHEDULER_K8S_TOKEN_PATH", s.token_path)
    s.ca_path = _str(env, "SCHEDULER_K8S_CA_PATH", s.ca_path)
    s.k8s_timeout_sec = _int(env, "SCHEDULER_K8S_TIMEOUT_SEC", s.k8s_timeout_sec)

    s.default_namespace = _str(env, "DEFAULT_NAMESPACE", s.default_namespace)
    s.allowed_namespaces = _list(env, "OPENVRE_ALLOWED_NAMESPACES") or [s.default_namespace]

    s.shared_pvc = _str(env, "DEFAULT_SHARED_PVC")
    s.userdata_pvc = _str(env, "DEFAULT_USERDATA_PVC") or s.shared_pvc
    s.admindata_pvc = _str(env, "DEFAULT_ADMINDATA_PVC") or s.shared_pvc
    s.tools_pvc = _str(env, "DEFAULT_TOOLS_PVC")
    extra_pvcs = _list(env, "OPENVRE_ALLOWED_PVCS")
    s.allowed_pvcs = sorted({p for p in [s.shared_pvc, s.userdata_pvc, s.tools_pvc, *extra_pvcs] if p})
    s.shared_data_prefix = (_str(env, "OPENVRE_SHARED_DATA_PREFIX", "/shared_data").rstrip("/") or "/shared_data")

    s.strict = _bool(env, "OPENVRE_STRICT", False)
    s.image_allowlist = _list(env, "OPENVRE_IMAGE_ALLOWLIST")
    # Whole registries or registry namespaces: every image below them is allowed,
    # so a new image there needs no config change.
    s.allowed_registries = _list(env, "OPENVRE_ALLOWED_REGISTRIES")
    s.image_pull_secrets = _list(env, "OPENVRE_IMAGE_PULL_SECRETS")
    s.allowed_pull_secrets = sorted(set(s.image_pull_secrets + _list(env, "OPENVRE_ALLOWED_PULL_SECRETS")))

    s.external_base = _str(env, "OPENVRE_EXTERNAL_BASE").rstrip("/")
    s.auth_url = _str(env, "OPENVRE_INTERACTIVE_AUTH_URL")
    s.interactive_default_image = _str(env, "OPENVRE_INTERACTIVE_DEFAULT_IMAGE")
    s.interactive_default_port = _str(env, "OPENVRE_INTERACTIVE_DEFAULT_PORT")
    s.interactive_default_mount = _str(env, "OPENVRE_INTERACTIVE_DEFAULT_MOUNT")
    s.node_selector = _str(env, "OPENVRE_NODE_SELECTOR")
    s.tolerations = _str(env, "OPENVRE_TOLERATIONS")
    s.preferred_hostname = _str(env, "OPENVRE_PREFERRED_HOSTNAME")
    s.eviction_toleration_seconds = _int(env, "OPENVRE_EVICTION_TOLERATION_SECONDS", 60) or 60
    s.session_ttl_hours = _int(env, "OPENVRE_SESSION_TTL_HOURS", 0)
    s.max_sessions_per_user = _int(env, "OPENVRE_MAX_SESSIONS_PER_USER", 0)
    s.reaper_interval_sec = _int(env, "OPENVRE_REAPER_INTERVAL_SEC", 300)
    s.max_session_cpu = _str(env, "OPENVRE_MAX_SESSION_CPU")
    s.max_session_memory = _str(env, "OPENVRE_MAX_SESSION_MEMORY")

    s.max_job_deadline_sec = _int(env, "OPENVRE_MAX_JOB_DEADLINE_SEC", s.max_job_deadline_sec)
    s.default_job_ttl_sec = _int(env, "OPENVRE_DEFAULT_JOB_TTL_SEC", s.default_job_ttl_sec)
    # UID/GID batch Jobs run as. Set here, not by the caller, because it decides
    # which files on the shared volume a Job can read and write.
    s.job_run_as_uid = _int(env, "OPENVRE_JOB_RUN_AS_UID", 1000)
    s.job_run_as_gid = _int(env, "OPENVRE_JOB_RUN_AS_GID", 1000)

    default_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
    s.template_dir = _str(env, "OPENVRE_TEMPLATE_DIR") or default_dir
    # Off: POST /jobs only accepts {template, params}. On: raw manifests are also
    # accepted (still checked by the Job policy). Only for moving off old frontends.
    s.allow_raw_job_manifests = _bool(env, "OPENVRE_ALLOW_RAW_JOB_MANIFESTS", False)

    s.audit_enabled = _bool(env, "OPENVRE_INTERACTIVE_AUDIT_SIDECAR_ENABLED", False)
    s.audit_image = _str(env, "OPENVRE_INTERACTIVE_AUDIT_IMAGE")
    s.audit_interval_sec = _str(env, "OPENVRE_INTERACTIVE_AUDIT_INTERVAL_SEC", "5") or "5"
    s.audit_base = (_str(env, "OPENVRE_INTERACTIVE_AUDIT_BASE", "/shared_data/admindata").rstrip("/") or "/shared_data/admindata")

    # How long the audit follows a new session before recording it as not ready,
    # and how many log lines per container go into each snapshot.
    s.audit_watch_sec = _int(env, "OPENVRE_INTERACTIVE_AUDIT_WATCH_SEC", 900)
    s.audit_log_tail_lines = _int(env, "OPENVRE_INTERACTIVE_AUDIT_LOG_LINES", 200)

    s.log_level = (_str(env, "SCHEDULER_LOG_LEVEL", "INFO") or "INFO").upper()

    check(s)
    return s


def check(s):
    problems = []
    if not s.auth_tokens:
        problems.append("SCHEDULER_AUTH_TOKEN is empty; every request would be rejected")
    if s.user_token not in ("off", "optional", "required"):
        problems.append("OPENVRE_USER_TOKEN must be off, optional or required")
    if s.user_token != "off" and not s.oidc_issuer:
        problems.append("OPENVRE_OIDC_ISSUER is required when OPENVRE_USER_TOKEN is not off")
    for t in s.auth_tokens:
        if len(t) < 16:
            problems.append("SCHEDULER_AUTH_TOKEN values must be at least 16 characters")
            break
    if len(s.gateway_token) < 16:
        problems.append("OPENVRE_GATEWAY_TOKEN must be set (at least 16 characters): the gateway reads session routes with it")
    elif s.gateway_token in s.auth_tokens:
        problems.append("OPENVRE_GATEWAY_TOKEN must differ from SCHEDULER_AUTH_TOKEN")
    if s.default_namespace not in s.allowed_namespaces:
        problems.append("DEFAULT_NAMESPACE must be listed in OPENVRE_ALLOWED_NAMESPACES")
    if s.audit_enabled and not s.audit_image:
        # The old default (python:3.11-alpine) exits at once and leaves sessions Pending forever.
        problems.append("OPENVRE_INTERACTIVE_AUDIT_IMAGE is required when the audit sidecar is enabled")
    try:
        s.image_rules = validation.compile_image_rules(s.image_allowlist, s.allowed_registries)
    except validation.ValidationError as e:
        problems.append(f"image allowlist: {e.message}")
    if s.strict and not (s.image_allowlist or s.allowed_registries):
        problems.append("OPENVRE_STRICT=1 needs OPENVRE_ALLOWED_REGISTRIES or OPENVRE_IMAGE_ALLOWLIST")
    if s.session_ttl_hours < 0 or s.max_sessions_per_user < 0:
        problems.append("OPENVRE_SESSION_TTL_HOURS and OPENVRE_MAX_SESSIONS_PER_USER must be >= 0")
    if s.reaper_interval_sec < 30:
        problems.append("OPENVRE_REAPER_INTERVAL_SEC must be at least 30")
    if s.job_run_as_uid < 1 or s.job_run_as_gid < 1:
        problems.append("OPENVRE_JOB_RUN_AS_UID and OPENVRE_JOB_RUN_AS_GID must be non-root (>= 1)")
    if problems:
        raise ConfigError("; ".join(problems))


def is_placeholder_token(s):
    return any(t.upper().startswith("CHANGEME") for t in s.auth_tokens)
