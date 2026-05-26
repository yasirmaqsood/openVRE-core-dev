# OpenVRE Scheduler (batch jobs + interactive sessions)

**Branch:** `kubernetes-interactive-pod-with-auth`  
**Image tag (Helm default):** `scheduler-1.1-interactive-auth`

Manages Kubernetes batch Jobs and interactive sessions (Deployment, Service, Ingress).

## Build

```bash
docker build -t <registry>/openvre-kubernetes:scheduler-1.1-interactive-auth kubernetes-deploy/scheduler
docker push <registry>/openvre-kubernetes:scheduler-1.1-interactive-auth
```

## Environment

| Variable | Purpose |
|----------|---------|
| `DEFAULT_NAMESPACE` | Namespace for Jobs and interactive resources |
| `SCHEDULER_AUTH_TOKEN` | Bearer token required from frontend |
| `OPENVRE_INTERACTIVE_AUTH_URL` | Set when Helm `interactive.openvreAuth.enabled: true` (ingress auth-url + auth-signin; RStudio DISABLE_AUTH). |
| `OPENVRE_INTERACTIVE_AUTH_SIGNIN` | Set when Helm `interactive.openvreAuth.enabled: true` (ingress auth-url + auth-signin; RStudio DISABLE_AUTH). |

## API

Batch: `POST/GET/DELETE /jobs` — Interactive: `POST/GET/DELETE /interactive-sessions` — Health: `GET /healthz` (no auth).

## Behaviour on this branch

Set when Helm `interactive.openvreAuth.enabled: true` (ingress auth-url + auth-signin; RStudio DISABLE_AUTH).

Helm chart: `kubernetes-deploy/openvre-helm-chart` — use tag `scheduler-1.1-interactive-auth`.
