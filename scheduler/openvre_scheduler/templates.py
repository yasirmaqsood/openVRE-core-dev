"""Workload templates: YAML files that define what a caller may launch.

A caller sends a template name and parameters. The scheduler checks each
parameter against the type the template declares, then fills the values into
the template's objects. The caller never sends a manifest, so it cannot set
the image, volumes, user or security settings unless the template exposes
them as parameters.

Two kinds of template:
  kind: Job                 one batch Job (manifest:)
  kind: InteractiveSession  the objects of one interactive session (objects:)

Placeholders are replaced as values in the parsed data, never as text in the
file, so a parameter value cannot change the structure:

    "{{ name }}"          the whole string becomes the value, keeping its type
                          (number, list, mapping). If the value is empty
                          (null), the key or list item is left out.
    "a {{ name }} b"      text placeholder: only text-like values
    "{{ config.key }}"    a value from the scheduler's own configuration
    "{{ session.key }}"   a value the scheduler computes for a session
                          (InteractiveSession templates only)
    - "{{ ...name }}"     inside a list: insert the items of a list value here

The file format and every parameter type are described in scheduler/README.md.
"""

import logging
import os
import re

import yaml

from . import validation as v

log = logging.getLogger("openvre_scheduler.templates")

PLACEHOLDER = re.compile(r"\{\{\s*(\.\.\.)?([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")
WHOLE = re.compile(r"^\{\{\s*(\.\.\.)?([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

PARAM_TYPES = {"string", "int", "bool", "quantity", "path", "image", "name", "env", "pull_secrets", "headers"}
LIST_TYPES = {"env", "pull_secrets"}
TEXT_TYPES = {"string", "int", "quantity", "path", "image", "name"}
PARAM_KEYS = {"type", "description", "required", "default", "max_length", "enum", "pattern",
              "min", "max", "under", "prefix", "max_items"}

# {{ config.* }}: name -> "text" (usable inside text), "list" (spreadable) or "any" (whole value only)
CONFIG_REFS = {
    "shared_pvc": "text", "shared_data_prefix": "text", "userdata_pvc": "text",
    "run_as_uid": "text", "run_as_gid": "text",
    "node_selector": "any", "affinity": "any", "tolerations": "list",
}

# {{ session.* }}: computed per session by sessions.building_blocks()
SESSION_REFS = {
    "name": "text", "path": "text", "url": "text", "namespace": "text", "user_id": "text",
    "labels": "any", "pod_labels": "any", "annotations": "any", "readiness_probe": "any",
    "command": "any", "gateway_annotations": "any",
    "mounts": "list", "audit_containers": "list", "audit_volumes": "list", "pull_secrets": "list",
}

# Session parameters with a fixed meaning: the scheduler's building blocks read them.
# A session template must declare "port"; the others are optional.
SESSION_RESERVED = {
    "port": "int",                  # app port: Service, probe, Guacamole connection
    "image": "image",
    "cpu": "quantity",              # capped by OPENVRE_MAX_SESSION_CPU
    "memory": "quantity",           # capped by OPENVRE_MAX_SESSION_MEMORY
    "executable": "string",         # -> session.command
    "readiness_path": "string",     # -> session.readiness_probe (HTTP instead of TCP)
    "proxy_root_mode": "string",    # -> session.gateway_annotations (request headers)
    "proxy_root_header": "string",
    "proxy_headers": "headers",
    "image_pull_secrets": "pull_secrets",  # -> session.pull_secrets
}
# Read by building blocks, so they count as used even when the file never names them.
SESSION_IMPLICIT = {"port", "executable", "readiness_path", "proxy_root_mode", "proxy_root_header",
                    "proxy_headers", "image_pull_secrets"}

SESSION_OBJECT_KINDS = {
    "ConfigMap": "v1",
    "Deployment": "apps/v1",
    "Service": "v1",
}
ACCESS_TYPES = {"web", "guacamole"}


class TemplateError(Exception):
    """A template file is invalid. Raised at startup."""


# YAML loading ----------------------------------------------------------------

class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys instead of keeping the last one."""


def _no_duplicates(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def load_yaml(text, source="<template>"):
    try:
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as e:
        raise TemplateError(f"{source}: invalid YAML: {e}")


# Parameters ------------------------------------------------------------------

class Param:
    def __init__(self, name, spec, source):
        where = f"{source}: parameter {name!r}"
        if not isinstance(spec, dict):
            raise TemplateError(f"{where} must be a mapping")
        unknown = set(spec) - PARAM_KEYS
        if unknown:
            raise TemplateError(f"{where}: unknown keys {sorted(unknown)}")
        self.name = name
        self.type = spec.get("type")
        if self.type not in PARAM_TYPES:
            raise TemplateError(f"{where}: type must be one of {sorted(PARAM_TYPES)}")
        self.description = spec.get("description", "")
        self.required = bool(spec.get("required", False))
        self.has_default = "default" in spec
        self.max_length = int(spec.get("max_length", 256))
        self.enum = spec.get("enum")
        self.pattern = re.compile(spec["pattern"]) if spec.get("pattern") else None
        self.min = spec.get("min")
        self.max = spec.get("max")
        self.under = spec.get("under")
        self.prefix = spec.get("prefix", "")
        self.max_items = int(spec.get("max_items", 50))
        if self.type == "path" and not (isinstance(self.under, str) and self.under.startswith("/")):
            raise TemplateError(f"{where}: type path needs 'under: /some/folder'")
        if self.required and self.has_default:
            raise TemplateError(f"{where}: a required parameter cannot have a default")
        if self.has_default:
            try:
                # Catches YAML type surprises early, e.g. an unquoted 0640 read as the number 416.
                self.default = self.check(spec["default"], settings=None)
            except v.ValidationError as e:
                raise TemplateError(f"{where}: invalid default: {e.message}")

    def describe(self):
        out = {"type": self.type, "required": self.required}
        for key in ("description", "enum", "min", "max", "under", "prefix"):
            val = getattr(self, key)
            if val not in (None, "", []):
                out[key] = val
        if self.has_default:
            out["default"] = self.default
        return out

    def empty(self):
        if self.type in LIST_TYPES:
            return []
        if self.type == "headers":
            return {}
        return ""

    def check(self, value, settings):
        """Validate one caller value and return it in its final form."""
        n, t = self.name, self.type
        if t == "string":
            # Strictly a string: never turn a number into text. This is what catches
            # YAML reading an unquoted 0640 as 416 in a template default.
            v.require(isinstance(value, str), f"{n} must be a string")
            value = v.plain_text(value, n, self.max_length)
            if self.pattern and value != "" and not self.pattern.fullmatch(value):
                raise v.ValidationError(f"{n} does not match the allowed pattern")
        elif t == "int":
            v.require(isinstance(value, int) and not isinstance(value, bool), f"{n} must be an integer")
            if self.min is not None:
                v.require(value >= self.min, f"{n} must be at least {self.min}")
            if self.max is not None:
                v.require(value <= self.max, f"{n} must be at most {self.max}")
        elif t == "bool":
            v.require(isinstance(value, bool), f"{n} must be true or false")
        elif t == "quantity":
            v.require(isinstance(value, str), f"{n} must be a string such as \"500m\" or \"2Gi\"")
            value = v.quantity(value, n)
            if self.max:
                v.at_most(value, str(self.max), n)
        elif t == "path":
            value = v.path_under(value, self.under, n)
        elif t == "image":
            value = v.image(value, settings, log) if settings else v.plain_text(value, n, 512, required=True)
        elif t == "name":
            value = v.dns_label(value, n)
            v.require(value.startswith(self.prefix), f"{n} must start with {self.prefix!r}")
        elif t == "env":
            v.require(isinstance(value, dict), f"{n} must be a mapping of NAME: value")
            v.require(len(value) <= self.max_items, f"{n} has more than {self.max_items} entries")
            out = []
            for key, val in value.items():
                v.require(isinstance(key, str) and _ENV_NAME.match(key), f"{n}: invalid variable name {key!r}")
                v.require(isinstance(val, (str, int, float)) and not isinstance(val, bool),
                          f"{n}.{key} must be a string or number")
                out.append({"name": key, "value": str(val)})
            value = out
        elif t == "headers":
            v.require(isinstance(value, dict), f"{n} must be a mapping of Header-Name: value")
            v.require(len(value) <= self.max_items, f"{n} has more than {self.max_items} entries")
            value = v.header_map(value, n)
        elif t == "pull_secrets":
            v.require(isinstance(value, list), f"{n} must be a list of secret names")
            value = [{"name": s} for s in (v.pull_secrets(value, settings) if settings else value)]
        if self.enum is not None and t in TEXT_TYPES | {"bool"}:
            v.require(value in self.enum, f"{n} must be one of {self.enum}")
        return value


# Shared template machinery ---------------------------------------------------

def config_values(settings):
    """Values a template may read as {{ config.<key> }}."""
    from . import manifests
    return {
        "shared_pvc": settings.shared_pvc,
        "shared_data_prefix": settings.shared_data_prefix,
        "userdata_pvc": settings.userdata_pvc,
        "run_as_uid": settings.job_run_as_uid,
        "run_as_gid": settings.job_run_as_gid,
        "node_selector": manifests.parse_node_selector(settings.node_selector),
        "affinity": manifests.affinity(settings),
        "tolerations": manifests.tolerations(settings),
    }


_DROP = object()


def fill(node, lookup):
    """Replace placeholders in parsed YAML. A whole placeholder whose value is None
    removes its key (in a mapping) or item (in a list)."""
    if isinstance(node, dict):
        out = {}
        for k, val in node.items():
            filled = fill(val, lookup)
            if filled is not _DROP:
                out[k] = filled
        return out
    if isinstance(node, list):
        out = []
        for item in node:
            m = WHOLE.match(item) if isinstance(item, str) else None
            if m and m.group(1):
                out.extend(lookup(m.group(2)) or [])
                continue
            filled = fill(item, lookup)
            if filled is not _DROP:
                out.append(filled)
        return out
    if isinstance(node, str):
        m = WHOLE.match(node)
        if m:
            value = lookup(m.group(2))
            return _DROP if value is None else value
        return PLACEHOLDER.sub(lambda mm: str(lookup(mm.group(2))), node)
    return node


class _Base:
    TOP_KEYS = set()
    REQUIRED_KEYS = set()
    ALLOW_SESSION_REFS = False

    def __init__(self, data, source):
        unknown = set(data) - self.TOP_KEYS
        if unknown:
            raise TemplateError(f"{source}: unknown keys {sorted(unknown)}")
        missing = self.REQUIRED_KEYS - set(data)
        if missing:
            raise TemplateError(f"{source}: missing keys {sorted(missing)}")
        self.source = source
        self.name = data["name"]
        try:
            v.dns_label(self.name, "template name")
        except v.ValidationError as e:
            raise TemplateError(f"{source}: {e.message}")
        self.kind = data["kind"]
        self.version = str(data.get("version", "1"))
        self.description = data.get("description", "")
        if not isinstance(data["parameters"], dict):
            raise TemplateError(f"{source}: parameters must be a mapping")
        self.params = {n: Param(n, spec, source) for n, spec in data["parameters"].items()}

    # Placeholder checks --------------------------------------------------

    def _ref_category(self, ref, path):
        """'text', 'list' or 'any' for a placeholder reference; error if unknown."""
        if ref.startswith("config."):
            key = ref[len("config."):]
            if key not in CONFIG_REFS:
                raise TemplateError(f"{self.source}: {path}: unknown config value {ref!r}")
            return CONFIG_REFS[key]
        if ref.startswith("session."):
            key = ref[len("session."):]
            if not self.ALLOW_SESSION_REFS:
                raise TemplateError(f"{self.source}: {path}: {ref!r} is only for InteractiveSession templates")
            if key not in SESSION_REFS:
                raise TemplateError(f"{self.source}: {path}: unknown session value {ref!r}")
            return SESSION_REFS[key]
        if ref not in self.params:
            raise TemplateError(f"{self.source}: {path}: unknown parameter {ref!r}")
        t = self.params[ref].type
        if t in LIST_TYPES:
            return "list"
        return "text" if t in TEXT_TYPES else "any"

    def check_placeholders(self, node, path, used):
        def visit(node, path, in_list):
            if isinstance(node, dict):
                for k, val in node.items():
                    if not isinstance(k, str) or PLACEHOLDER.search(k):
                        raise TemplateError(f"{self.source}: {path}: keys must be plain text")
                    visit(val, f"{path}.{k}", False)
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    visit(item, f"{path}[{i}]", True)
            elif isinstance(node, str):
                whole = WHOLE.match(node)
                for m in PLACEHOLDER.finditer(node):
                    spread, ref = m.group(1), m.group(2)
                    cat = self._ref_category(ref, path)
                    if spread:
                        if not (whole and in_list):
                            raise TemplateError(f"{self.source}: {path}: '{{{{ ...{ref} }}}}' must be a whole list item")
                        if cat != "list":
                            raise TemplateError(f"{self.source}: {path}: '...{ref}' needs a list value")
                    elif not whole and cat != "text":
                        raise TemplateError(f"{self.source}: {path}: {ref} cannot be used inside other text")
                    if "." not in ref:
                        used.add(ref)

        visit(node, path, False)

    def check_unused(self, used, implicit=()):
        unused = set(self.params) - used - set(implicit)
        if unused:
            raise TemplateError(f"{self.source}: parameters never used: {sorted(unused)}")

    def describe(self):
        return {"name": self.name, "version": self.version, "description": self.description, "kind": self.kind,
                "parameters": {n: p.describe() for n, p in self.params.items()}}

    def resolve_params(self, raw, settings):
        """Check caller parameters: no unknown ones, required ones present, each one valid."""
        v.require(isinstance(raw, dict), "params must be a JSON object")
        unknown = set(raw) - set(self.params)
        v.require(not unknown, f"unknown parameters for template {self.name}: {sorted(unknown)}")
        out = {}
        for name, p in self.params.items():
            if name in raw and raw[name] is not None:
                out[name] = p.check(raw[name], settings)
            elif p.required:
                raise v.ValidationError(f"parameter {name} is required")
            elif p.has_default:
                out[name] = p.default
            else:
                out[name] = p.empty()
        return out


# Job templates ---------------------------------------------------------------

class JobTemplate(_Base):
    TOP_KEYS = {"name", "version", "description", "kind", "name_param", "parameters", "manifest"}
    REQUIRED_KEYS = {"name", "kind", "name_param", "parameters", "manifest"}

    def __init__(self, data, source):
        super().__init__(data, source)
        self.name_param = data["name_param"]
        np = self.params.get(self.name_param)
        if np is None or np.type != "name" or not np.required or not np.prefix:
            raise TemplateError(f"{source}: name_param must be a required parameter of type name with a prefix")
        self.manifest = data["manifest"]
        if not isinstance(self.manifest, dict) or self.manifest.get("kind") != "Job" \
                or self.manifest.get("apiVersion") != "batch/v1":
            raise TemplateError(f"{source}: manifest must be apiVersion batch/v1, kind Job")
        m = WHOLE.match(str((self.manifest.get("metadata") or {}).get("name", "")))
        if not m or m.group(1) or m.group(2) != self.name_param:
            raise TemplateError(f"{source}: manifest.metadata.name must be \"{{{{ {self.name_param} }}}}\"")
        used = set()
        self.check_placeholders(self.manifest, "manifest", used)
        self.check_unused(used)

    def render(self, raw_params, settings):
        values = self.resolve_params(raw_params, settings)
        cfg = config_values(settings)
        manifest = fill(self.manifest, lambda ref: cfg[ref[7:]] if ref.startswith("config.") else values[ref])
        labels = manifest.setdefault("metadata", {}).setdefault("labels", {})
        labels["openvre.io/template"] = self.name
        return manifest, values[self.name_param]


# Interactive session templates -----------------------------------------------

class SessionTemplate(_Base):
    TOP_KEYS = {"name", "version", "description", "kind", "access", "parameters", "objects"}
    REQUIRED_KEYS = {"name", "kind", "access", "parameters", "objects"}
    ALLOW_SESSION_REFS = True

    def __init__(self, data, source):
        super().__init__(data, source)
        for pname, ptype in SESSION_RESERVED.items():
            p = self.params.get(pname)
            if p is not None and p.type != ptype:
                raise TemplateError(f"{source}: parameter {pname!r} has a fixed meaning and must be type {ptype}")
        if "port" not in self.params:
            raise TemplateError(f"{source}: session templates must declare the parameter 'port' (type int)")

        self.access = data["access"]
        if not isinstance(self.access, dict) or self.access.get("type") not in ACCESS_TYPES:
            raise TemplateError(f"{source}: access.type must be one of {sorted(ACCESS_TYPES)}")
        if set(self.access) - {"type", "protocol"}:
            raise TemplateError(f"{source}: access only takes 'type' and 'protocol'")
        if self.access["type"] == "guacamole" and not self.access.get("protocol"):
            raise TemplateError(f"{source}: access.protocol is required for guacamole (e.g. vnc, rdp)")

        self.objects = data["objects"]
        if not isinstance(self.objects, list) or not self.objects:
            raise TemplateError(f"{source}: objects must be a non-empty list")
        deployments = 0
        for i, obj in enumerate(self.objects):
            where = f"{source}: objects[{i}]"
            if not isinstance(obj, dict) or obj.get("kind") not in SESSION_OBJECT_KINDS:
                raise TemplateError(f"{where}: kind must be one of {sorted(SESSION_OBJECT_KINDS)}")
            if obj.get("apiVersion") != SESSION_OBJECT_KINDS[obj["kind"]]:
                raise TemplateError(f"{where}: {obj['kind']} needs apiVersion {SESSION_OBJECT_KINDS[obj['kind']]}")
            name = str((obj.get("metadata") or {}).get("name", ""))
            m = WHOLE.match(name)
            if m and not m.group(1) and m.group(2) == "session.name":
                if obj["kind"] == "Deployment":
                    deployments += 1
            elif not re.match(r"^\{\{\s*session\.name\s*\}\}-[a-z0-9-]+$", name):
                raise TemplateError(f"{where}: metadata.name must be \"{{{{ session.name }}}}\" or "
                                    f"\"{{{{ session.name }}}}-suffix\"")
        if deployments != 1:
            raise TemplateError(f"{source}: exactly one Deployment named \"{{{{ session.name }}}}\" is required")

        used = set()
        self.check_placeholders(self.objects, "objects", used)
        self.check_placeholders(self.access, "access", used)
        self.check_unused(used, SESSION_IMPLICIT)

    def describe(self):
        out = super().describe()
        out["access"] = {"type": self.access["type"]}
        out["objects"] = [o["kind"] for o in self.objects]
        return out

    def render(self, values, settings, blocks):
        """Fill the objects. values: resolved parameters; blocks: session.* values."""
        cfg = config_values(settings)

        def lookup(ref):
            if ref.startswith("config."):
                return cfg[ref[7:]]
            if ref.startswith("session."):
                return blocks[ref[8:]]
            return values[ref]

        objects = fill(self.objects, lookup)
        access = fill(self.access, lookup)
        return objects, access


# Loading ---------------------------------------------------------------------

KINDS = {"Job": JobTemplate, "InteractiveSession": SessionTemplate}


def Template(data, source):  # noqa: N802 - reads like a class at call sites
    """Build the right template class for the file's kind."""
    if not isinstance(data, dict):
        raise TemplateError(f"{source}: the file must be a mapping")
    cls = KINDS.get(data.get("kind"))
    if cls is None:
        raise TemplateError(f"{source}: kind must be one of {sorted(KINDS)}")
    return cls(data, source)


def load_dir(path):
    """Load and check every *.yaml / *.yml / *.json file in a folder. Fails on the first bad file."""
    if not os.path.isdir(path):
        raise TemplateError(f"template folder {path} does not exist")
    templates = {}
    for fname in sorted(os.listdir(path)):
        if fname.startswith(".") or not fname.endswith((".yaml", ".yml", ".json")):
            continue
        full = os.path.join(path, fname)
        if not os.path.isfile(full):
            continue
        with open(full, "r", encoding="utf-8") as f:
            text = f.read()
        if len(text) > 256 * 1024:
            raise TemplateError(f"{fname}: larger than 256 KiB")
        tpl = Template(load_yaml(text, fname), fname)
        stem = fname.rsplit(".", 1)[0]
        if tpl.name != stem:
            raise TemplateError(f"{fname}: name {tpl.name!r} must match the file name")
        if tpl.name in templates:
            raise TemplateError(f"{fname}: template {tpl.name!r} defined twice")
        templates[tpl.name] = tpl
    if not templates:
        raise TemplateError(f"no templates found in {path}")
    return templates
