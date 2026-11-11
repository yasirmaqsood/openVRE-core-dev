# OpenVRE scheduler

A small HTTP API between the OpenVRE frontend and Kubernetes. The frontend asks it to create, check and delete:

- **batch Jobs** (tool runs, launcher `kubernetes_native`)
- **interactive sessions** (RStudio, Stata, Guacamole desktops): a Deployment, a Service, and for web tools an Ingress plus a ConfigMap of proxy headers

The scheduler holds the Kubernetes credentials, so the frontend never needs any. It is stateless: Kubernetes is the only record of what runs.

**Everything is launched from YAML templates** (`templates/`): batch Jobs and interactive sessions. The caller sends a template name and parameters, never a Kubernetes manifest. A new tool is a tool definition in OpenVRE, not a code change. See [Templates](#templates) and [Adding a new tool](#adding-a-new-tool).

Python 3.11, standard library plus PyYAML (templates) and PyJWT with cryptography (user token signatures).

```
browser → frontend (PHP, ProcessK8s.php) ──HTTP + JSON, Bearer token──▶ scheduler :8080 ──▶ Kubernetes API
```

## Layout

| Path | What it holds |
|---|---|
| `app.py` | Entrypoint (`python app.py`), kept so existing images and manifests work |
| `openvre_scheduler/config.py` | All environment variables, checked once at startup |
| `openvre_scheduler/server.py` | HTTP server, routing, auth, logging |
| `openvre_scheduler/validation.py` | Checks for every value coming from a request |
| `openvre_scheduler/templates.py` | Loading, checking and filling the YAML workload templates |
| `openvre_scheduler/policy.py` | What a filled-in Job or session may contain (checked after every template) |
| `templates/` | The workload templates shipped in the image: `openvre-batch-job`, `openvre-web-session`, `openvre-desktop-session` |
| `requirements.txt` | PyYAML, pinned |
| `openvre_scheduler/jobs.py` | Batch Job create / status / delete |
| `openvre_scheduler/sessions.py` | Interactive session request checks, create / status / delete / expiry |
| `openvre_scheduler/manifests.py` | Session building blocks (`{{ session.* }}`) and node placement helpers |
| `openvre_scheduler/audit.py` | Admin-only audit files for sessions |
| `openvre_scheduler/k8s.py` | Minimal Kubernetes API client |
| `audit-sidecar/` | Separate image: the audit sidecar that runs inside session pods |
| `tests/` | Unit and HTTP tests (`make test`) |

## Authentication

Every request (except `/healthz` and `/readyz`) passes two independent checks.

**1. Which service is calling:** the shared bearer token `SCHEDULER_AUTH_TOKEN`, compared in constant time. Several comma-separated tokens can be valid at once, for rotation. With user tokens required (below), this token on its own cannot read or change any session or Job.

**2. On whose behalf** (`OPENVRE_USER_TOKEN`: `off`, `optional`, `required`):

The frontend sends the logged-in user's Keycloak access token in `X-OpenVRE-User-Token`, refreshing it when it expires. The scheduler checks signature, issuer (`OPENVRE_OIDC_ISSUER`), expiry and client (`azp` = `OPENVRE_OIDC_CLIENT_ID`) against Keycloak's public keys (`OPENVRE_OIDC_JWKS_URL`, default `<issuer>/protocol/openid-connect/certs`). Then:

- New sessions and Jobs get the label `openvre.io/owner` (a hash of issuer and user `sub`, no personal data).
- Reading or deleting a session or Job is allowed only for its owner, or for users with a role in `OPENVRE_OIDC_ADMIN_ROLES`. Others get "does not exist" / 404.
- The per-user session limit counts the verified owner, not the `user_id` in the request body.
- The audit record stores the verified `owner_sub`, `owner_username` and `owner_email`.
- With `required`, requests without a valid user token get 401, and objects created before user tokens (no owner label) are reachable only by admin roles.

The scheduler needs to reach Keycloak for its keys (chart: `scheduler.hostAliases`, `scheduler.oidcEgressCidrs`). Keys are cached for an hour and refetched when an unknown key id appears. If the keys cannot be fetched, requests fail with 503; they are never let through.

What a compromised frontend can still do: act for users whose tokens it currently holds (logged-in users, for minutes), within the templates, policies and allowlists. It can no longer act as arbitrary users, act while nobody is logged in, or stop other users' sessions.

## API

Every route also works under `/v1/...`. Every response is JSON with `"ok": true|false`; errors carry `"error"` and `"request_id"`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | no | Liveness. Always 200 while the process runs. |
| GET | `/readyz` | no | Readiness. 200 if the Kubernetes API answers (checked at most every 10 s), else 503. |
| GET | `/templates` | yes | List templates with their parameters (type, required, default, limits). |
| GET | `/templates/<name>` | yes | One template. |
| POST | `/jobs` | yes | Create a batch Job: `{"namespace": "...", "template": "openvre-batch-job", "params": {...}}`. Returns `name`, `template`. |
| GET | `/jobs/<name>?namespace=` | yes | `{"exists", "status": {"state", "reason"}, "job": "<raw Job JSON string>"}` |
| DELETE | `/jobs/<name>?namespace=` | yes | Delete a Job. Already gone is still `ok`. |
| POST | `/interactive-sessions` | yes | Create a session: `{"namespace", "template", "context": {...}, "params": {...}}`. Returns `name`, `access`, `access_url`, `path_prefix`, `expires_at`. |
| GET | `/interactive-sessions/<name>?namespace=` | yes | `{"exists", "state": "Running|Pending|Failed", "reason"}` |
| DELETE | `/interactive-sessions/<name>?namespace=` | yes | Delete a session. Already gone is still `ok`. |

Status codes:

| Code | Meaning |
|---|---|
| 200 / 201 | OK / created |
| 400 | Bad input (invalid name, path, JSON, quantity, unknown or missing template parameter, ...) |
| 401 | Missing or wrong token |
| 403 | Not allowed (namespace, PVC, image with `OPENVRE_STRICT=1`, pull secret) |
| 404 / 405 | Unknown route or template / wrong method |
| 409 | Object already exists in Kubernetes |
| 413 | Body larger than `SCHEDULER_MAX_BODY_BYTES` |
| 422 | Job manifest rejected by policy, or Kubernetes rejected the object |
| 429 | Per-user session limit reached |
| 502 | Kubernetes API returned an error |
| 503 | Kubernetes API unreachable |

Names are always checked: Jobs must start with `openvre-bj-`, sessions with `openvre-ix-`, both lowercase DNS labels. Reads and deletes only touch objects carrying the OpenVRE labels, so the API cannot reach platform objects such as `scheduler` or `dashboard-frontend`.

### Session request body

Sent by `ProcessK8s::createSession()`:

```json
{
  "namespace": "fedcomp-openvre",
  "template": "openvre-web-session",
  "context": {
    "user_id": "alice", "tool_id": "rstudio", "tool_name": "RStudio", "session_id": "rstudio-1a2b3c4d",
    "project": "proj", "execution": "run1",
    "working_dir": "/shared_data/userdata/alice/proj/run1",
    "workspace_mount_path": "/home/rstudio",
    "volume_mounts": [{"host_path": "/shared_data/userdata/alice/proj/rstudio_data", "container_path": "/home/rstudio"}],
    "external_base": "https://vre.example.org"
  },
  "params": {"image": "ghcr.io/inab/k8-tool-rstudio:1.0", "port": 8787, "proxy_root_mode": "custom_header", "proxy_root_header": "X-RStudio-Root-Path"}
}
```

**`context`**: set by OpenVRE, the same for every template. Checked by fixed rules:

| Field | Required | Notes |
|---|---|---|
| `user_id` | yes | Stored as annotation, and as label `openvre.io/user` (hashed if it does not fit a label) |
| `tool_id`, `tool_name` | no | Used for the session name and audit folder |
| `session_id` | no | Kept as metadata only. **The scheduler picks the session name.** |
| `project`, `execution` | no | Audit metadata |
| `working_dir` | yes | Strictly below `OPENVRE_SHARED_DATA_PREFIX` (not its root), not inside `OPENVRE_INTERACTIVE_AUDIT_BASE` |
| `volume_mounts` | no | `[{host_path, container_path, read_only}]`, same path rules as `working_dir`, at most 20. Empty: `working_dir` is mounted at `workspace_mount_path` |
| `workspace_mount_path` | no | Default `OPENVRE_INTERACTIVE_DEFAULT_MOUNT` or `/workspace` |
| `external_base` | no | Ignored when `OPENVRE_EXTERNAL_BASE` is set (the frontend fills it from the Host header) |

Unknown context fields are rejected.

**`params`**: the tool's own settings, checked against the parameters the template declares (`GET /v1/templates/<name>`). Unknown parameters are rejected.

**Response `access`** tells the frontend how users reach the session, as declared by the template:

```json
{"type": "web", "url": "/interactive-tool/rstudio-5f2c9a1b7e30/"}
{"type": "guacamole", "host": "openvre-ix-stata-5f2c9a1b7e30", "port": 5901, "protocol": "vnc"}
```

## Templates

A template is a YAML file that defines one kind of workload a caller may launch, and the inputs it accepts. Templates live in `templates/` (copied into the image at `/app/templates`). Set `OPENVRE_TEMPLATE_DIR` to load them from somewhere else, e.g. a ConfigMap mounted into the scheduler pod.

The flow, here for a batch Job (sessions work the same way with `POST /v1/interactive-sessions`):

```
frontend ──POST /v1/jobs {"template": "openvre-batch-job", "params": {...}}──▶ scheduler
   1. find the template            (unknown name → 404)
   2. check each parameter         (unknown, missing or invalid → 400 / 403)
   3. fill the values in           ({{ name }} and {{ config.key }})
   4. run the Job policy           (second check, see below → 422)
   5. create the Job in Kubernetes
```

The caller only ever sends values. The image registry rules, user and group, volumes, node placement and security settings are in the template or in the scheduler's configuration, so a caller cannot change them.

### Shipped templates

| Template | Kind | Used by | What it creates |
|---|---|---|---|
| `openvre-batch-job` | Job | Every `kubernetes_native` tool run | A Job: the tool image runs the submission script the frontend wrote into the run folder |
| `openvre-web-session` | InteractiveSession | Browser tools: RStudio, Jupyter, VS Code, Stata web, ... | ConfigMap (proxy headers), Deployment, Service, Ingress at `/interactive-tool/<session>/` |
| `openvre-desktop-session` | InteractiveSession | Desktop tools through Guacamole (VNC or RDP) | Deployment, Service; access through Guacamole |

Session template parameters (`web` / `desktop`):

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `image` | image | required | Checked against the allowed images (see [Allowed images](#allowed-images)) |
| `port` | int | required | App port (web) or VNC/RDP port (desktop) |
| `cpu` | quantity | `"500m"` / `"1"`, max `"8"` | Request and limit; also capped by `OPENVRE_MAX_SESSION_CPU` |
| `memory` | quantity | `"1536Mi"` / `"2Gi"`, max `"16Gi"` | Also capped by `OPENVRE_MAX_SESSION_MEMORY` |
| `executable` | string | `""` | Start command (`sh -lc ...`); empty keeps the image's own |
| `readiness_path` | string | `""` | HTTP path answering when ready; empty means a TCP check |
| `env` | env | `{}` | Extra environment variables |
| `image_pull_secrets` | pull_secrets | `[]` | Must be in `OPENVRE_ALLOWED_PULL_SECRETS` |
| `proxy_root_mode` | string (web) | `""` | `x_forwarded_prefix`, `custom_header`, `none` |
| `proxy_root_header` | string (web) | `""` | Header for `custom_header`, e.g. `X-RStudio-Root-Path` |
| `proxy_headers` | headers (web) | `{}` | Extra request headers; `{{PATH_PREFIX}}` becomes the session path |
| `protocol` | string (desktop) | `"vnc"` | `vnc` or `rdp` |

`openvre-batch-job` parameters (also visible at `GET /v1/templates/openvre-batch-job`):

| Parameter | Type | Required / default | Notes |
|---|---|---|---|
| `job_name` | name | required | Must start with `openvre-bj-`; OpenVRE's job id |
| `image` | image | required | Checked against the allowed images (see [Allowed images](#allowed-images)) |
| `workdir` | path | required | Under `/shared_data` |
| `submit_script` | path | required | Under `/shared_data` |
| `cpu` | quantity | `"1"`, max `"8"` | Request and limit |
| `memory_request` | quantity | `"1Gi"`, max `"16Gi"` | |
| `memory_limit` | quantity | `"2Gi"`, max `"16Gi"` | |
| `deadline_seconds` | int | `86400`, 60 to 604800 | `activeDeadlineSeconds` |
| `ttl_seconds` | int | `120`, 0 to 604800 | Delete the finished Job after this |
| `env` | env | `{}`, at most 50 | Tool variables (`infrastructure.container_env`) |
| `image_pull_secrets` | pull_secrets | `[]` | Must be in `OPENVRE_ALLOWED_PULL_SECRETS` |

From the scheduler config, not the caller: `runAsUser`/`runAsGroup`/`fsGroup` (`OPENVRE_JOB_RUN_AS_UID/GID`), the shared PVC and mount path, `nodeSelector` (`OPENVRE_NODE_SELECTOR`), tolerations (`OPENVRE_TOLERATIONS` plus eviction).

Example request:

```json
{
  "namespace": "fedcomp-openvre",
  "template": "openvre-batch-job",
  "params": {
    "job_name": "openvre-bj-alice-tool1-run-1a2b3c4d",
    "image": "ghcr.io/inab/fem-tool:1.0",
    "workdir": "/shared_data/userdata/alice/proj/run1",
    "submit_script": "/shared_data/userdata/alice/proj/run1/.submit",
    "cpu": "2",
    "memory_request": "4Gi",
    "memory_limit": "4Gi",
    "env": {"THREADS": "2"}
  }
}
```

### Writing a template

```yaml
name: my-template            # must match the file name (my-template.yaml)
description: What it runs and who calls it.
kind: Job                    # or InteractiveSession (see "Session templates")
name_param: job_name         # the parameter that becomes metadata.name

parameters:
  job_name: {type: name, prefix: my-prefix-, required: true}
  input:    {type: path, under: /shared_data/userdata, required: true}
  mode:     {type: string, default: "0640", enum: ["0640", "0644"]}

manifest:                    # an ordinary Kubernetes manifest with placeholders
  apiVersion: batch/v1
  kind: Job
  metadata:
    name: "{{ job_name }}"
  spec:
    activeDeadlineSeconds: 3600
    template:
      spec:
        securityContext:
          runAsUser: "{{ config.run_as_uid }}"
        containers:
          - name: main
            image: ghcr.io/inab/some-tool:1.0      # fixed: the caller cannot change it
            args: ["{{ input }}", "{{ mode }}"]
```

**Placeholders.** Values are filled into the parsed YAML, never pasted into the file text, so a parameter value cannot change the manifest's structure.

| Form | Meaning |
|---|---|
| `"{{ name }}"` (the whole string) | Replaced by the value with its type kept: number, list, mapping |
| `"text {{ name }} text"` | Text placeholder. Only for string, int, quantity, path, image and name parameters |
| `"{{ config.key }}"` | Scheduler configuration: `shared_pvc`, `shared_data_prefix`, `run_as_uid`, `run_as_gid`, `node_selector`, `tolerations` |
| `- "{{ ...name }}"` (a list item) | Insert the items of a list value (env, pull_secrets, `config.tolerations`) at this point |

**Parameter types.**

| Type | Accepts | Options |
|---|---|---|
| `string` | Text (a real string, not a number) | `max_length` (256), `pattern` (regex, whole value), `enum` |
| `int` | Whole number | `min`, `max`, `enum` |
| `bool` | `true` / `false` | |
| `quantity` | Kubernetes quantity as a string: `"500m"`, `"2Gi"` | `max` |
| `path` | Absolute path, no `..`, normalized | `under` (required) |
| `image` | Image reference | Checked against the allowed images (see [Allowed images](#allowed-images)) |
| `name` | Lowercase DNS label, at most 63 characters | `prefix` |
| `env` | Mapping `NAME: value` → `[{name, value}]` | `max_items` (50) |
| `pull_secrets` | List of secret names → `[{name}]` | Names must be in `OPENVRE_ALLOWED_PULL_SECRETS` |

Every parameter also takes `description`, `required: true` or a `default`. Callers may not send parameters the template does not declare.

**Checked at startup.** The scheduler refuses to start (exit code 2, a `CRITICAL` log line naming the file) if any template:

- is not valid YAML, or has a **duplicate key** (normally the last one silently wins)
- has an unknown top-level or parameter key, or an unknown parameter type
- has a `default` that does not fit its type. This catches YAML's type guessing: an unquoted `0640` is read as the number 416 and is rejected for a `string` parameter.
- uses a placeholder for a parameter or config key that does not exist, or declares a parameter it never uses
- uses a list value inside text, or `...` anywhere other than a whole list item
- has a `name` that does not match its file name, or `metadata.name` other than `"{{ <name_param> }}"`

**Rules of thumb.** Quote every placeholder (`"{{ x }}"`; unquoted `{{` starts a YAML mapping). Quote values that must stay text: `"1"`, `"0640"`, `"true"`, `"1.10"`. Put the image in the template, not in a parameter, unless the caller really must choose it.

### Session templates

`kind: InteractiveSession` templates describe the objects of one session. They differ from Job templates in these ways:

```yaml
name: openvre-web-session
version: "1"                       # recorded on every session (annotation openvre.io/template-version)
kind: InteractiveSession
access:                            # how users reach the session
  type: web                        # web, or guacamole (then also protocol: "{{ protocol }}")
parameters: {...}                  # must declare port (int)
objects:                           # ConfigMap, Deployment, Service, Ingress, in creation order
  - apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: "{{ session.name }}"   # exactly one Deployment with this name
  - ...                            # other objects: "{{ session.name }}" or "{{ session.name }}-suffix"
```

**Session building blocks.** The parts that need logic are computed by the scheduler and placed with `{{ session.* }}`:

| Value | Use | What it is |
|---|---|---|
| `session.name` | text | `openvre-ix-<tool>-<12 hex>` |
| `session.path` / `session.url` | text | `/interactive-tool/<tool>-<hex>` / with trailing `/` |
| `session.namespace`, `session.user_id` | text | |
| `session.labels` | mapping | `app`, `openvre.io/interactive`, `openvre.io/session`, `openvre.io/user`, `openvre.io/template`, managed-by |
| `session.pod_labels` | mapping | Same as `session.labels` |
| `session.annotations` | mapping | session id, user id, created/expires, audit path, template version |
| `session.mounts` | list | volumeMounts of the `userdata` volume, from `context.volume_mounts` (subPaths computed and checked) |
| `session.audit_containers` / `session.audit_volumes` | list | The audit sidecar and the `admindata` volume; empty when audit is off. Use with `...` |
| `session.readiness_probe` | mapping | TCP on `port`, or HTTP on `readiness_path` |
| `session.command` | list or empty | `["sh", "-lc", executable]`; empty (key left out) when there is no executable |
| `session.pull_secrets` | list | `OPENVRE_IMAGE_PULL_SECRETS` plus the `image_pull_secrets` parameter |
| `session.proxy_headers` | mapping | Data for the `<session>-proxy-hdr` ConfigMap |
| `session.ingress_host` | text or empty | Host from `OPENVRE_EXTERNAL_BASE`; empty leaves `host` out |
| `session.ingress_path` | text | `<path>(/|$)(.*)` (pairs with the rewrite annotation) |
| `session.ingress_annotations` | mapping | nginx timeouts, rewrite, priority, auth-url/signin, redirects |

Session templates can also read `{{ config.userdata_pvc }}`, `{{ config.ingress_class }}` and `{{ config.affinity }}`, besides the `config.*` values listed above.

**Parameters with a fixed meaning.** The building blocks read these names, so if a session template declares them they must have this type: `port` (int, required), `image` (image), `cpu`, `memory` (quantity, capped by the session limits), `executable`, `readiness_path`, `proxy_root_mode`, `proxy_root_header` (string), `proxy_headers` (headers), `image_pull_secrets` (pull_secrets).

**Fixed names.** The user data volume must be named `userdata` (`session.mounts` refers to it), and the proxy header ConfigMap `<session>-proxy-hdr` (the scheduler links the Ingress to it).

**Extra startup checks for session templates:** objects only of the four allowed kinds; exactly one Deployment named `{{ session.name }}`; every other object named `{{ session.name }}` or `{{ session.name }}-suffix`; `port` declared; a valid `access` block; `{{ session.* }}` only with known names, and only in session templates.

### Adding a new tool

Most new tools need **no code change and no new template**: only a tool definition in OpenVRE (MongoDB), under `infrastructure.k8s`. The frontend forwards `template` and `params` to the scheduler as they are.

```json
"infrastructure": {
  "interactive": true,
  "workspace_mount_path": "/home/jovyan/work",
  "k8s": {
    "template": "openvre-web-session",
    "params": {
      "image": "ghcr.io/inab/jupyter:1.0",
      "port": 8888,
      "readiness_path": "/api",
      "env": {"JUPYTER_ENABLE_LAB": "yes"},
      "proxy_root_mode": "x_forwarded_prefix"
    }
  }
}
```

1. Pick the template by how users reach the tool: a browser app → `openvre-web-session`; a desktop over VNC/RDP → `openvre-desktop-session`.
2. Put the tool's values in `params`. `GET /v1/templates/<name>` lists what each template accepts.
3. If `OPENVRE_STRICT=1`, add the image to `OPENVRE_IMAGE_ALLOWLIST`; for a private registry, add its secret to `OPENVRE_ALLOWED_PULL_SECRETS`.

Write a **new template** only when a tool needs a different *structure*: an extra container, an init container, a GPU node, another volume type, a different access method. Name it after the pattern (`gpu-web-session`), not the tool, so the next tool like it can reuse it. Put it in the template folder; the scheduler checks it at startup. A new Kubernetes object kind or access type needs a code change (policy and frontend).

Tools without `infrastructure.k8s` keep working: the frontend maps their older fields (`container_image`, `container_port`, `interactive_access`, `proxy`, `executable`, ...) to `openvre-web-session` or, with `interactive_access.mode: guacamole`, to `openvre-desktop-session`.

## Allowed images

With `OPENVRE_STRICT=1`, every image in a Job or session (tool images, init containers, images fixed in a template) must be allowed. The configured audit sidecar image is always allowed.

**Allow a whole registry or namespace** with `OPENVRE_ALLOWED_REGISTRIES`, so new images there need no config change:

```
OPENVRE_ALLOWED_REGISTRIES=registry.gitlab.bsc.es/impact-cohorte,docker.io/ymaqsoodbsc,ghcr.io/inab
```

| Entry | Allows |
|---|---|
| `registry.gitlab.bsc.es` | Every image on that registry |
| `registry.gitlab.bsc.es/impact-cohorte` | Every image in that group, including sub-groups |
| `docker.io/ymaqsoodbsc` or `ymaqsoodbsc` | Every image of that Docker Hub account |
| `ghcr.io/inab/*` | Same as `ghcr.io/inab` (the `*` is optional) |

**Allow single images** with `OPENVRE_IMAGE_ALLOWLIST`: `ghcr.io/inab/tool` (any tag), `ghcr.io/inab/tool:1.0` (that tag only), `ghcr.io/inab/tool@sha256:...` (that digest only). Entries ending in `/` or `*` work like registries.

How matching works:

- Images are normalized the way Docker does: no registry means `docker.io`, a single name means `docker.io/library/<name>`, no tag means `:latest`. So `ymaqsoodbsc/fem-tool` and `docker.io/ymaqsoodbsc/fem-tool:latest` are the same image.
- Registries and namespaces match on whole path segments: `registry.gitlab.bsc.es/impact-cohorte` allows `.../impact-cohorte/x` but **not** `.../impact-cohorte-evil/x` or `registry.gitlab.bsc.es.evil.com/...`.
- `docker.io` on its own (all of Docker Hub) is refused at startup: it would allow any public image.
- The scheduler does not contact the registry. An image that does not exist there fails to pull, as before.
- Invalid image references are always rejected, strict or not.

Allowing a registry namespace means trusting everyone who can push to it. Keep push access to those namespaces limited to people who maintain tools.

## Session policy

Session templates can grow over time, so every filled-in session is checked before anything is created. Rejected sessions return 422 with every reason.

- Only ConfigMap, Deployment, Service and Ingress; every object named after the session.
- Deployment: `replicas` forced to 1, selector forced to the session; no `hostNetwork`, `hostPID`, `hostIPC`, `serviceAccountName`, `nodeName`, `runtimeClassName`, ...; volumes only allowed PVCs, `emptyDir`, or the session's own ConfigMaps (no `secret`, `projected`, `hostPath`, ...); the admin volume only in the audit sidecar; with a single shared PVC, tool containers must mount a sub-folder outside the audit folder; containers and init containers: no `privileged`, no added capabilities, no `hostPort`, no `envFrom` or Secret-reading env, images through the allowlist (the configured audit image is exempt); `automountServiceAccountToken` and `enableServiceLinks` forced off.
- Service: `ClusterIP` only, no `nodePort`, selector forced to the session.
- Ingress: no `*snippet*` annotations (they run raw nginx config), no `tls` or `defaultBackend`, backends must be the session Service; class, host and the scheduler's annotations (auth, redirects, proxy headers) are forced.
- ConfigMap: string data only.
- Every object gets the session labels and the request namespace.

## Batch Job policy

Every Job, whether filled in from a template or (only if `OPENVRE_ALLOW_RAW_JOB_MANIFESTS=1`) sent as a raw manifest, passes this policy before it is created. A mistake in a template therefore cannot produce a privileged or Secret-reading pod. The scheduler accepts the Job only if:

- it is `batch/v1` `Job`, named `openvre-bj-...`, in the request namespace
- volumes are only `persistentVolumeClaim` (on the allowed PVC list) or `emptyDir`; no `secret`, `projected`, `configMap`, `hostPath`, ...
- the pod sets no `serviceAccountName`, `hostNetwork`, `hostPID`, `hostIPC`, `nodeName`, `initContainers`, `ephemeralContainers`, `runtimeClassName`, `priorityClassName`, ...
- containers have no `privileged`, no added capabilities, no `envFrom`, no `env.valueFrom` reading Secrets or ConfigMaps
- the pod `securityContext.runAsUser` is set and is not 0
- `activeDeadlineSeconds` is set, between 1 and `OPENVRE_MAX_JOB_DEADLINE_SEC`
- images pass the allowlist (see `OPENVRE_STRICT`)
- `imagePullSecrets` are on the allowed list

It then forces: `automountServiceAccountToken: false`, `enableServiceLinks: false`, `runAsNonRoot`, seccomp `RuntimeDefault`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, the OpenVRE labels including `openvre.io/batch: "true"` (the batch NetworkPolicy selects it), and the default pull secrets.

A rejected Job returns 422 with every reason in `error`.

## Interactive sessions

Each session is named `openvre-ix-<tool>-<12 random hex>` and served at `/interactive-tool/<tool>-<hex>/`. The frontend Apache rule maps that path back to the Service `openvre-ix-<tool>-<hex>`. What objects a session has comes from its template; the shipped templates give pods:

- `automountServiceAccountToken: false`, `enableServiceLinks: false` (forced by the policy for any template)
- a readiness probe (TCP on the port, or HTTP `readiness_path`), so `Running` means the app answers
- `strategy: Recreate`, so two copies never use the same workspace
- labels `openvre.io/interactive`, `openvre.io/session`, `openvre.io/user`, `openvre.io/template`, `app.kubernetes.io/managed-by: openvre-scheduler`
- annotations `openvre.io/session-id`, `openvre.io/user-id`, `openvre.io/created-at`, `openvre.io/template-version`, `openvre.io/expires-at` (with a TTL), `openvre.io/audit-host-path` (with audit)
- the tool container keeps running as the image user (RStudio's init needs root); `fsGroup` is `OPENVRE_JOB_RUN_AS_GID`; no seccomp profile, because desktop apps in Guacamole sessions may need syscalls `RuntimeDefault` blocks

**Status.** Read from the Deployment named after the session. `state` is `Failed` when a container waits with `ImagePullBackOff`, `ErrImagePull`, `CrashLoopBackOff` or a container config error; `reason` says which. The frontend shows that as `ERROR`.

**Delete.** Finds every object with the label `openvre.io/session=<name>`, plus the fixed names older sessions used, and deletes those carrying `openvre.io/interactive`. Objects without that label are never touched.

**Expiry.** With `OPENVRE_SESSION_TTL_HOURS` > 0, every session gets `openvre.io/expires-at`, and a background loop (every `OPENVRE_REAPER_INTERVAL_SEC`) deletes expired sessions. Sessions created before the TTL was set have no expiry and are left alone.

**Per-user limit.** With `OPENVRE_MAX_SESSIONS_PER_USER` > 0, a new session for a user who already has that many returns 429. Counted from the `openvre.io/user` label, so sessions created before this version are not counted.

### Audit record

With the audit sidecar on, every session has a folder `<OPENVRE_INTERACTIVE_AUDIT_BASE>/<tool>/<user>/<session>/` that stays after the session is gone. It answers "who ran what, and why did it fail".

`session-meta.json` holds the outcome:

| Field | Meaning |
|---|---|
| `status` | `starting` → `running`, or `failed`. At delete: `stopped`; a failure stays `failed`. |
| `reason`, `message` | Why it failed: `ErrImagePull`, `ImagePullBackOff`, `CrashLoopBackOff`, `FailedCreate` (quota or policy rejected the pod), `NotReadyInTime`, ... and the Kubernetes message |
| `created_at`, `ready_at`, `failed_at`, `deleted_at`, `delete_reason` | Timeline (`delete_reason`: `delete` or `expired`) |
| user, tool, template, image, project, run folder | Who ran what |

After creation, a background watcher follows the session until it is ready, fails, or `OPENVRE_INTERACTIVE_AUDIT_WATCH_SEC` passes, then saves a snapshot. Another snapshot is saved just before delete, so the last logs survive the pod:

| File | Content |
|---|---|
| `<kind>.json` | The objects as created |
| `k8s-events-<phase>.json` | Events of every object named after the session (pods, ReplicaSets, Deployment, ...). Kubernetes itself keeps events for one hour only. |
| `containers-<phase>.json` | Each container's state, reason, exit code, restarts, last termination (e.g. `OOMKilled`) |
| `pod-<phase>.json`, `deployment-live-<phase>.json` | Live objects |
| `logs/<phase>/<pod>__<container>.log` | Last `OPENVRE_INTERACTIVE_AUDIT_LOG_LINES` lines (and `__previous` after a restart) |
| `events.jsonl`, `latest-state.json` | Workspace file changes, from the sidecar |

`<phase>` is `running`, `failed`, `timeout` or `delete`. The watcher runs inside the scheduler: if the scheduler restarts while a session is starting, that session gets no running/failed snapshot, but the delete snapshot still records its state.

## Configuration

Read once at startup. The scheduler refuses to start (exit code 2, a `CRITICAL` log line) if the configuration is invalid.

| Variable | Default | Purpose |
|---|---|---|
| `SCHEDULER_AUTH_TOKEN` | **required** | Bearer token, at least 16 characters. Comma-separated list allowed, for rotation (add new, roll frontend, remove old). A value starting with `CHANGEME` logs a warning. |
| `OPENVRE_USER_TOKEN` | `off` | `off`, `optional` or `required` |
| `OPENVRE_OIDC_ISSUER` | | Keycloak realm URL, e.g. `http://auth.vre.local/auth/realms/demoproject`; required unless user tokens are off |
| `OPENVRE_OIDC_JWKS_URL` | `<issuer>/protocol/openid-connect/certs` | Keycloak public keys |
| `OPENVRE_OIDC_CLIENT_ID` | | Expected `azp` (or audience) of user tokens, e.g. `open-vre` |
| `OPENVRE_OIDC_ADMIN_ROLES` | | Keycloak roles that may act on other users' sessions and Jobs |
| `SCHEDULER_LISTEN_PORT` | `8080` | Listen port (not `SCHEDULER_PORT`, which Kubernetes sets for the `scheduler` Service) |
| `SCHEDULER_MAX_BODY_BYTES` | `1048576` | Largest accepted request body |
| `SCHEDULER_REQUEST_TIMEOUT_SEC` | `30` | Socket timeout per client connection |
| `SCHEDULER_K8S_TIMEOUT_SEC` | `30` | Timeout per Kubernetes API call |
| `SCHEDULER_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DEFAULT_NAMESPACE` | `default` | Namespace when a request gives none |
| `OPENVRE_ALLOWED_NAMESPACES` | `DEFAULT_NAMESPACE` | Comma list of namespaces requests may use |
| `DEFAULT_SHARED_PVC` | | Shared data PVC; allowed in Jobs and sessions |
| `DEFAULT_USERDATA_PVC` | shared PVC | PVC mounted into session pods |
| `DEFAULT_ADMINDATA_PVC` | shared PVC | PVC for audit files (sidecar only, never the tool container) |
| `DEFAULT_TOOLS_PVC` | | Optional extra PVC allowed in Jobs |
| `OPENVRE_ALLOWED_PVCS` | | Comma list of further PVCs Jobs and sessions may use |
| `OPENVRE_SHARED_DATA_PREFIX` | `/shared_data` | Paths in requests must be under this; it maps to the PVC root |
| `OPENVRE_STRICT` | off | `1`: images not allowed are rejected (403). Off: they are allowed and logged. |
| `OPENVRE_ALLOWED_REGISTRIES` | | Comma list of registries or registry namespaces; every image below them is allowed. See [Allowed images](#allowed-images). |
| `OPENVRE_IMAGE_ALLOWLIST` | | Comma list of single images: `repo` (any tag), `repo:tag`, `repo@sha256:...`. For images outside the allowed registries. |
| `OPENVRE_IMAGE_PULL_SECRETS` | | Secrets added to every Job and session pod |
| `OPENVRE_ALLOWED_PULL_SECRETS` | | Further secrets a tool may ask for (`infrastructure.image_pull_secrets`) |
| `OPENVRE_INGRESS_CLASS` | | Ingress class for web sessions (required unless all sessions are Guacamole) |
| `OPENVRE_EXTERNAL_BASE` | | Public URL (`https://host`); sets the Ingress host and redirects |
| `OPENVRE_INTERACTIVE_AUTH_URL` / `_SIGNIN` | | nginx `auth-url` / `auth-signin` for session Ingresses |
| `OPENVRE_INTERACTIVE_DEFAULT_IMAGE` / `_PORT` / `_MOUNT` | | Defaults for session requests |
| `OPENVRE_MAX_SESSION_CPU` / `_MEMORY` | | Upper limits for one session (e.g. `4`, `16Gi`) |
| `OPENVRE_SESSION_TTL_HOURS` | `0` (off) | Delete sessions this many hours after creation |
| `OPENVRE_REAPER_INTERVAL_SEC` | `300` | How often expired sessions are checked (min 30) |
| `OPENVRE_MAX_SESSIONS_PER_USER` | `0` (off) | Concurrent sessions per user |
| `OPENVRE_MAX_JOB_DEADLINE_SEC` | `604800` | Upper limit for a Job's `activeDeadlineSeconds` |
| `OPENVRE_TEMPLATE_DIR` | `/app/templates` | Folder of workload templates (`*.yaml`, `*.yml`, `*.json`) |
| `OPENVRE_ALLOW_RAW_JOB_MANIFESTS` | off | `1`: `POST /jobs` also accepts `{"manifest": {...}}` (still policy checked). Only for moving off old frontends. |
| `OPENVRE_JOB_RUN_AS_UID` / `_GID` | `1000` | User and group batch Jobs and the audit sidecar run as; GID is also the session `fsGroup` (`{{ config.run_as_uid/gid }}`). Must not be 0 |
| `OPENVRE_NODE_SELECTOR` | | `key=value,...` for session pods and batch Jobs |
| `OPENVRE_TOLERATIONS` | | `key[=value]:Effect,...` for session pods and batch Jobs |
| `OPENVRE_PREFERRED_HOSTNAME` | | Preferred node for session pods |
| `OPENVRE_EVICTION_TOLERATION_SECONDS` | `60` | not-ready / unreachable eviction wait |
| `OPENVRE_INTERACTIVE_AUDIT_SIDECAR_ENABLED` | off | Add the audit sidecar to session pods |
| `OPENVRE_INTERACTIVE_AUDIT_IMAGE` | | **Required when audit is on** (the old default `python:3.11-alpine` exited at once and left sessions Pending) |
| `OPENVRE_INTERACTIVE_AUDIT_INTERVAL_SEC` | `5` | Sidecar scan interval |
| `OPENVRE_INTERACTIVE_AUDIT_BASE` | `/shared_data/admindata` | Audit folder root, mounted in the scheduler pod |
| `OPENVRE_INTERACTIVE_AUDIT_WATCH_SEC` | `900` | How long a new session is followed before it is recorded as `NotReadyInTime` |
| `OPENVRE_INTERACTIVE_AUDIT_LOG_LINES` | `200` | Log lines per container in each snapshot |

`OPENVRE_AUDIT_RUN_AS_UID` / `_GID` are no longer read; the scheduler's UID comes from its pod `securityContext`.

## Logs

One JSON object per line on stdout. Every request logs `request_id`, `method`, `path`, `status`, `duration_ms`, `client`. The `X-Request-ID` request header is reused if present and always returned. Kubernetes errors are logged in full; clients get a short message.

## Running and testing

```bash
make test              # 155 unit + HTTP tests (needs requirements.txt)
make image             # docker build -t openvre-scheduler:dev .
```

Kubernetes probes for the scheduler Deployment:

```yaml
livenessProbe:
  httpGet: {path: /healthz, port: 8080}
  periodSeconds: 20
readinessProbe:
  httpGet: {path: /readyz, port: 8080}
  periodSeconds: 10
```

The scheduler is stateless, so it can run with more than one replica. The per-user session limit is checked per replica, so two simultaneous requests on two replicas can exceed it by one.

## Upgrading from the single-file scheduler (v1)

- **Upgrade the scheduler and the frontend together.** The frontend now sends `{"template": "openvre-batch-job", "params": {...}}` to `/v1/jobs`; v1 expected a YAML manifest at `/jobs`. A v1 frontend gets 400 from this scheduler, and this frontend gets 404 from a v1 scheduler. If you cannot upgrade both at once, set `OPENVRE_ALLOW_RAW_JOB_MANIFESTS=1` for the transition; the old YAML still is not accepted, only JSON manifests.
- Batch Job user/group now comes from the scheduler (`OPENVRE_JOB_RUN_AS_UID/GID`, default 1000), and node selector and tolerations from the scheduler's `OPENVRE_NODE_SELECTOR` / `OPENVRE_TOLERATIONS`. The frontend's `OPENVRE_K8S_NODE_SELECTOR`, `OPENVRE_K8S_TOLERATIONS` and `OPENVRE_K8S_TOOLS_PVC` are no longer read.
- Interactive sessions are now created from `openvre-web-session` / `openvre-desktop-session`. The frontend sends `{"template", "context", "params"}` to `/v1/interactive-sessions`; tools without `infrastructure.k8s` are mapped automatically, so no tool definition has to change. The frontend no longer sends `run_as_uid/gid`, `userdata_pvc` or `admindata_pvc`, and no longer reads `OPENVRE_K8S_RUN_AS_UID/GID`, `OPENVRE_K8S_USERDATA_PVC` or `OPENVRE_K8S_ADMINDATA_PVC`: the scheduler's `OPENVRE_JOB_RUN_AS_GID`, `DEFAULT_USERDATA_PVC` and `DEFAULT_ADMINDATA_PVC` apply.
- Sessions created before templates keep working for status and delete.
- Tool memory above the template's `max` (16Gi) is rejected; raise `max` in `templates/openvre-batch-job.yaml` if a tool needs more.
- Sessions and Jobs created by v1 keep working: status and delete accept their names and labels.
- `OPENVRE_STRICT`, `OPENVRE_ALLOWED_REGISTRIES` and `OPENVRE_IMAGE_ALLOWLIST` now take effect (v1 did not check images). They apply to **batch Jobs and sessions**. With strict on, check that every batch tool image (and `OPENVRE_K8S_JOB_IMAGE` on the frontend) is covered before upgrading. With strict off, nothing is blocked; other images are only logged.
- Batch Jobs no longer list the unused `tools` PVC volume, so `DEFAULT_TOOLS_PVC` is not needed.
- If the audit sidecar is on, `OPENVRE_INTERACTIVE_AUDIT_IMAGE` must be set or the scheduler will not start.
- New, all optional: `OPENVRE_ALLOWED_NAMESPACES`, `OPENVRE_IMAGE_PULL_SECRETS`, `OPENVRE_ALLOWED_PULL_SECRETS`, `OPENVRE_SESSION_TTL_HOURS`, `OPENVRE_MAX_SESSIONS_PER_USER`, `OPENVRE_MAX_SESSION_CPU/MEMORY`.
- Add the liveness and readiness probes above to the scheduler Deployment.
