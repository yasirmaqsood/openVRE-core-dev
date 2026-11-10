"""Checks for every value that comes from a request before it reaches Kubernetes."""

import hashlib
import posixpath
import re

JOB_PREFIX = "openvre-bj-"
SESSION_PREFIX = "openvre-ix-"

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_LABEL_VALUE = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")
_QUANTITY = re.compile(r"^([0-9]+(\.[0-9]+)?|\.[0-9]+)(m|k|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei)?$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ValidationError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def require(cond, message, status=400):
    if not cond:
        raise ValidationError(message, status)


def dns_label(value, what, max_len=63):
    value = value if isinstance(value, str) else ""
    require(0 < len(value) <= max_len and _DNS_LABEL.match(value), f"invalid {what}: must be lowercase letters, digits and '-', at most {max_len} characters")
    return value


def namespace(value, settings):
    ns = value or settings.default_namespace
    dns_label(ns, "namespace")
    require(ns in settings.allowed_namespaces, f"namespace {ns!r} is not allowed", 403)
    return ns


def job_name(value):
    dns_label(value, "job name")
    require(value.startswith(JOB_PREFIX), f"job name must start with {JOB_PREFIX!r}")
    return value


def session_name(value):
    dns_label(value, "session name")
    require(value.startswith(SESSION_PREFIX), f"session name must start with {SESSION_PREFIX!r}")
    return value


def plain_text(value, what, max_len=256, required=False):
    if value is None:
        value = ""
    require(isinstance(value, (str, int)), f"{what} must be a string")
    value = str(value).strip()
    require(not _CONTROL.search(value), f"{what} contains control characters")
    require(len(value) <= max_len, f"{what} is longer than {max_len} characters")
    if required:
        require(value != "", f"{what} is required")
    return value


def quantity(value, what):
    value = str(value).strip()
    require(_QUANTITY.match(value), f"invalid {what} {value!r}")
    return value


_SUFFIX = {"m": 1e-3, "": 1, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
           "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}


def quantity_value(value):
    m = re.match(r"^([0-9.]+)([a-zA-Z]*)$", str(value).strip())
    return float(m.group(1)) * _SUFFIX[m.group(2)]


def at_most(value, limit, what):
    if limit:
        require(quantity_value(value) <= quantity_value(limit), f"{what} {value} is above the limit {limit}")


def port(value, what="container_port"):
    try:
        p = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{what} must be a number")
    require(1 <= p <= 65535, f"{what} must be between 1 and 65535")
    return p


def uid(value, what):
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{what} must be a number")
    require(0 <= v <= 2**31 - 1, f"{what} is out of range")
    return v


def abs_path(value, what):
    """A normalized absolute path with no '..' segments."""
    value = plain_text(value, what, 1024, required=True)
    require(value.startswith("/"), f"{what} must be an absolute path")
    require(".." not in value.split("/"), f"{what} must not contain '..'")
    return posixpath.normpath(value)


def path_under(value, prefix, what):
    path = abs_path(value, what)
    prefix = prefix.rstrip("/")
    require(path == prefix or path.startswith(prefix + "/"), f"{what} must be under {prefix}")
    return path


def subpath(path, prefix):
    """Turn /shared_data/x/y into x/y for a PVC subPath."""
    prefix = prefix.rstrip("/")
    return "" if path == prefix else path[len(prefix) + 1:]


def pvc(value, settings, what="pvc"):
    dns_label(value, what, 253)
    require(value in settings.allowed_pvcs, f"{what} {value!r} is not an allowed volume", 403)
    return value


def header_map(value, what):
    if not value:
        return {}
    require(isinstance(value, dict), f"{what} must be an object")
    out = {}
    for k, v in value.items():
        require(isinstance(k, str) and _HEADER_NAME.match(k), f"invalid header name in {what}: {k!r}")
        out[k] = plain_text(v, f"{what}.{k}", 512)
    return out


def pull_secrets(value, settings):
    """Names requested by a tool, limited to OPENVRE_ALLOWED_PULL_SECRETS."""
    if not value:
        return []
    require(isinstance(value, list), "image_pull_secrets must be a list")
    out = []
    for name in value:
        dns_label(name, "image pull secret name", 253)
        require(name in settings.allowed_pull_secrets, f"image pull secret {name!r} is not allowed", 403)
        out.append(name)
    return out


# Image references ------------------------------------------------------------
#
# Images are compared in the normalized form Docker uses, so "python",
# "library/python" and "docker.io/library/python:latest" are the same image,
# and "ymaqsoodbsc/fem-tool" is "docker.io/ymaqsoodbsc/fem-tool:latest".

_PATH_COMPONENT = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_REGISTRY = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]+)?$")
DOCKER_HUB = "docker.io"


def _split_registry(ref):
    """(registry, rest). The first component is a registry if it looks like a host."""
    first, sep, rest = ref.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        registry = first.lower()
        if registry in ("index.docker.io", "registry-1.docker.io"):
            registry = DOCKER_HUB
        return registry, rest
    return DOCKER_HUB, ref


def parse_image(ref):
    """Normalize an image reference to (registry, repository path, tag, digest).
    Raises ValidationError if it is not a valid reference."""
    ref = str(ref).strip()
    name, _, digest = ref.partition("@")
    tag = ""
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = name.rsplit(":", 1)
    registry, path = _split_registry(name)
    require(_REGISTRY.match(registry), f"invalid image registry in {ref!r}")
    parts = path.split("/")
    require(path and all(_PATH_COMPONENT.match(p) for p in parts), f"invalid image name {ref!r}")
    if registry == DOCKER_HUB and len(parts) == 1:
        path = f"library/{path}"
    if tag:
        require(_TAG.match(tag), f"invalid image tag in {ref!r}")
    if digest:
        require(_DIGEST.match(digest), f"invalid image digest in {ref!r}")
    if not tag and not digest:
        tag = "latest"
    return registry, path, tag, digest


class ImageRule:
    """One allowlist entry.

    scope   registry, or registry/namespace: every image below it
            ("registry.gitlab.bsc.es/impact-cohorte", "ghcr.io/inab/*")
    repo    a repository, any tag ("ghcr.io/inab/k8-tool-rstudio")
    exact   one tag or digest ("ghcr.io/inab/k8-tool-rstudio:1.0", "...@sha256:...")
    """

    def __init__(self, entry, kind):
        self.entry = entry
        self.kind = kind
        if kind == "scope":
            raw = entry.strip().rstrip("*").rstrip("/")
            # "registry.gitlab.bsc.es" is a registry; "ymaqsoodbsc" is a Docker Hub namespace.
            registry, rest = _split_registry(raw + "/")
            rest = rest.strip("/")
            require(_REGISTRY.match(registry), f"invalid registry {entry!r}")
            require(not rest or all(_PATH_COMPONENT.match(p) for p in rest.split("/")), f"invalid registry path {entry!r}")
            self.registry, self.path = registry, rest
        else:
            self.registry, self.path, self.tag, self.digest = parse_image(entry)
            self.explicit = ("@" in entry) or (":" in entry.rsplit("/", 1)[-1])

    def matches(self, registry, path, tag, digest):
        if registry != self.registry:
            return False
        if self.kind == "scope":
            return not self.path or path == self.path or path.startswith(self.path + "/")
        if path != self.path:
            return False
        if not self.explicit:
            return True  # repository: any tag
        if self.digest:
            return digest == self.digest
        return tag == self.tag and not digest


def compile_image_rules(allowlist, registries):
    """Turn OPENVRE_IMAGE_ALLOWLIST and OPENVRE_ALLOWED_REGISTRIES into rules.
    Raises ValidationError for an invalid or too broad entry."""
    rules = []
    for entry in registries:
        rule = ImageRule(entry, "scope")
        require(not (rule.registry == DOCKER_HUB and not rule.path),
                f"{entry!r} would allow every public Docker Hub image; name the namespace, e.g. docker.io/yourorg")
        rules.append(rule)
    for entry in allowlist:
        if entry.endswith("*") or entry.endswith("/"):
            rule = ImageRule(entry, "scope")
            require(not (rule.registry == DOCKER_HUB and not rule.path),
                    f"{entry!r} would allow every public Docker Hub image")
        else:
            rule = ImageRule(entry, "image")
        rules.append(rule)
    return rules


def image_allowed(image, settings):
    """True if the image is covered by OPENVRE_ALLOWED_REGISTRIES or OPENVRE_IMAGE_ALLOWLIST."""
    try:
        ref = parse_image(image)
    except ValidationError:
        return False
    return any(rule.matches(*ref) for rule in settings.image_rules)


def image(value, settings, log=None):
    value = plain_text(value, "image", 512, required=True)
    require(" " not in value, "image must not contain spaces")
    parse_image(value)  # always a valid reference, strict or not
    if not image_allowed(value, settings):
        if settings.strict:
            raise ValidationError(
                f"image {value!r} is not allowed: not in OPENVRE_ALLOWED_REGISTRIES or OPENVRE_IMAGE_ALLOWLIST", 403)
        if log and settings.image_rules:
            log.warning("image not in allowed registries or allowlist (allowed because OPENVRE_STRICT is off)",
                        extra={"image": value})
    return value


def label_value(value, fallback_prefix="u"):
    """A Kubernetes label value for a user id. Ids that do not fit become a stable hash."""
    value = str(value)
    if len(value) <= 63 and _LABEL_VALUE.match(value):
        return value
    return f"{fallback_prefix}-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def segment(value, max_len, fallback):
    value = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")
    return (value[:max_len].rstrip("-")) or fallback
