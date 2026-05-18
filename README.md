# OpenVRE — branch `kubernetes-interactive-pod-with-auth`

Per-session interactive pods **plus** OpenVRE authorization: only the logged-in owner can open a session URL; **no RStudio password** (gateway signs in server-side).


## Before you install

Complete **[kubernetes-deploy/openvre-helm-chart/GETTING_STARTED.md](kubernetes-deploy/openvre-helm-chart/GETTING_STARTED.md)** (Sections 2, 4, 5, 7, 8, 9), then **[kubernetes-deploy/INTERACTIVE.md](kubernetes-deploy/INTERACTIVE.md)** for auth.

## Start here

| Doc | Purpose |
|-----|---------|
| [kubernetes-deploy/openvre-helm-chart/GETTING_STARTED.md](./kubernetes-deploy/openvre-helm-chart/GETTING_STARTED.md) | Install cluster from scratch |
| [kubernetes-deploy/openvre-helm-chart/README.md](./kubernetes-deploy/openvre-helm-chart/README.md) | Chart reference |
| [kubernetes-deploy/INTERACTIVE.md](./kubernetes-deploy/INTERACTIVE.md) | Interactive + auth (this variant) |
| [kubernetes-deploy/README.md](./kubernetes-deploy/README.md) | Deploy bundle layout |

## Extra PHP (vs `kubernetes-interactive-pod`)

| File | Role |
|------|------|
| `front_end/openVRE/public/applib/interactiveAuth.php` | Ingress `auth-url` |
| `front_end/openVRE/public/applib/interactiveGateway.php` | Auto RStudio login |
| `front_end/openVRE/public/applib/interactiveLoginStart.php` | Sign-in with return URL |
| `front_end/openVRE/public/applib/loginToken.php` | OAuth return to session |
| `front_end/openVRE/public/phplib/funclib.inc.php` | `sanitizeInteractiveUserPath()` |

`Tooljob.php` / `actions-home.js` use `/applib/interactiveGateway.php?path=...`.

## Helm

Set `interactive.openvreAuth.enabled: true` in `kubernetes-deploy/openvre-helm-chart/values.yaml`.

## Related branches

- **`kubernetes`** — batch only
- **`kubernetes-interactive-pod`** — pods without OpenVRE auth on URLs
