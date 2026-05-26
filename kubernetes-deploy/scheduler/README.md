# OpenVRE Scheduler (batch / `kubernetes_native` jobs)

Python service the frontend calls to create, inspect, and delete Kubernetes `batch/v1` Jobs.

**Branch:** batch-only (`kubernetes`, `kubernetes-with-deploy`).  
**Image tag (Helm default):** `scheduler-1.0`

## Build

From this directory:

```bash
docker build -t <registry>/openvre-kubernetes:scheduler-1.0 .
docker push <registry>/openvre-kubernetes:scheduler-1.0
```

From repo root on `kubernetes-with-deploy`:

```bash
docker build -t <registry>/openvre-kubernetes:scheduler-1.0 kubernetes-deploy/scheduler
```

## Environment

| Variable | Purpose |
|----------|---------|
| `DEFAULT_NAMESPACE` | Namespace for Jobs when not specified in the request |
| `SCHEDULER_AUTH_TOKEN` | Bearer token required from the frontend |
| `KUBERNETES_SERVICE_HOST` / `PORT` | Set automatically in-cluster |

ServiceAccount token and CA are read from `/var/run/secrets/kubernetes.io/serviceaccount/`.

## API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/healthz` | No | Health check |
| `POST` | `/jobs` | Yes | Create Job from YAML manifest |
| `GET` | `/jobs/<name>?namespace=` | Yes | Get Job |
| `DELETE` | `/jobs/<name>?namespace=` | Yes | Delete Job |

```text
Authorization: Bearer <SCHEDULER_AUTH_TOKEN>
```

## Helm

```yaml
scheduler:
  image:
    repository: <registry>/openvre-kubernetes
    tag: "scheduler-1.0"
```
