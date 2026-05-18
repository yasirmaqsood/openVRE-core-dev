# OpenVRE Scheduler

Standalone service the OpenVRE frontend calls for Kubernetes workloads.

On branch **`kubernetes-interactive-pod`** / **`kubernetes-interactive-pod-with-auth`**, `app.py` also manages **interactive sessions** (Deployments, Services, Ingresses).

## Build

From repo root:

```bash
docker build -t <registry>/openvre-kubernetes:scheduler-interactive kubernetes-deploy/scheduler
docker push <registry>/openvre-kubernetes:scheduler-interactive
```

## Runtime environment

| Variable | Purpose |
|----------|---------|
| `DEFAULT_NAMESPACE` | Namespace for Jobs / interactive resources |
| `SCHEDULER_AUTH_TOKEN` | Bearer token required from frontend |
| `OPENVRE_INTERACTIVE_AUTH_URL` | **Auth branch only** — ingress `auth-url` |
| `OPENVRE_INTERACTIVE_AUTH_SIGNIN` | **Auth branch only** — ingress `auth-signin` |

## API — batch jobs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/healthz` | Health check (no auth) |
| `POST` | `/jobs` | Create a Kubernetes Job |
| `GET` | `/jobs/<name>?namespace=` | Get Job |
| `DELETE` | `/jobs/<name>?namespace=` | Delete Job |

## API — interactive sessions

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/interactive-sessions` | Create Deployment + Service + Ingress + ConfigMap |
| `GET` | `/interactive-sessions/<name>?namespace=` | Session status |
| `DELETE` | `/interactive-sessions/<name>?namespace=` | Tear down all session resources |

All endpoints except `/healthz` require:

```text
Authorization: Bearer <SCHEDULER_AUTH_TOKEN>
```

## Pods vs auth

| | Pods branch | Auth branch |
|--|-------------|-------------|
| `DISABLE_AUTH` on RStudio pod | Yes | Yes |
| Ingress `auth-url` | No | Yes (when env set) |

See [../INTERACTIVE.md](../INTERACTIVE.md).

## Helm

```yaml
scheduler:
  image:
    repository: ymaqsoodbsc/openvre-kubernetes
    tag: scheduler-interactive
```

Chart templates: `../openvre-helm-chart/templates/scheduler.yaml`.
