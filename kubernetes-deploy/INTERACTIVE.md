# Interactive tools — with OpenVRE auth

> **Prerequisites:** Complete [openvre-helm-chart/GETTING_STARTED.md](openvre-helm-chart/GETTING_STARTED.md) Sections **2**, **4**, **8**, **5–7**, **9** before interactive + auth setup.

Branch: **`kubernetes-interactive-pod-with-auth`**

Same per-session pods as the pods-only branch, plus:

- **Ingress `auth-url`** → `front_end/.../applib/interactiveAuth.php`
- **Gateway** → `interactiveGateway.php` (server-side RStudio sign-in)
- **Sign-in redirect** → `interactiveLoginStart.php`

RStudio pods run with **`DISABLE_AUTH=true`**; the owner does not type an RStudio password.

## URL pattern

Workspace link (UI):

```text
http://<host>/applib/interactiveGateway.php?path=/<user>/<session>
```

After gateway + auth, user lands on:

```text
http://<host>/<user>/<session>/
```

## Components (this repo)

| Component | Path |
|-----------|------|
| Scheduler | `kubernetes-deploy/scheduler/app.py` |
| PHP launcher | `ProcessK8sInteractive.php` |
| Auth / gateway | `applib/interactiveAuth.php`, `interactiveGateway.php`, `interactiveLoginStart.php` |
| Helm | `kubernetes-deploy/openvre-helm-chart/` |

## Helm values

```yaml
interactive:
  openvreAuth:
    enabled: true
    authUrl: "http://dashboard-frontend.YOUR_NS.svc.cluster.local/applib/interactiveAuth.php"
    signInUrl: "http://YOUR_PUBLIC_HOST/applib/interactiveLoginStart.php"
```

Scheduler receives `OPENVRE_INTERACTIVE_AUTH_URL` and `OPENVRE_INTERACTIVE_AUTH_SIGNIN` when enabled in the chart.

## Build and deploy

### 1. Scheduler + frontend images

Same as pods variant; use tags that include this branch’s PHP (gateway + auth files).

### 2. Helm

```bash
cd kubernetes-deploy/openvre-helm-chart
helm upgrade --install openvre . -f my-values.yaml -n YOUR_NS
```

Confirm `interactive.openvreAuth.enabled: true` and URLs match your namespace and ingress host.

### 3. MongoDB

Same as pods variant (`kubernetes_native`, interactive RStudio tool).

## User flow

1. Launch RStudio → scheduler creates pod + ingress (with auth annotations).
2. **Access Session** → `interactiveGateway.php` (must be logged into OpenVRE).
3. Gateway validates user, signs in to RStudio internally, redirects to session path.
4. Further requests to `/<user>/<session>/` pass ingress auth (`interactiveAuth.php`).
5. **Stop Session** deletes K8s resources via scheduler.

## Compare variants

| | `kubernetes-interactive-pod` | This branch |
|--|------------------------------|-------------|
| OpenVRE login on URL | No | Yes |
| RStudio login | No (`DISABLE_AUTH`) | No (gateway) |
| `applib/interactive*.php` | No | Yes |

See also [../README.md](../README.md) (repo root).
