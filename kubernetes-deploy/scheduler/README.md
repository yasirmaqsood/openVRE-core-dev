# OpenVRE Scheduler Image

This folder contains the standalone scheduler service that the OpenVRE frontend calls to create, inspect, and delete Kubernetes `batch/v1` Jobs.

The Helm chart now uses the published scheduler image by default:

```text
ymaqsoodbsc/openvre-kubernetes:scheduler-1.0
```

## Build

From `k8s-deployments/bsc-tre-copy`:

```bash
docker build -t ymaqsoodbsc/openvre-kubernetes:scheduler-1.0 ./scheduler
```

Or with a registry:

```bash
docker build -t <registry>/openvre-kubernetes:scheduler-1.0 ./scheduler
docker push <registry>/openvre-kubernetes:scheduler-1.0
```

## Runtime configuration

The scheduler expects to run inside Kubernetes with a mounted ServiceAccount token. It reads:

| Variable | Purpose |
|---|---|
| `DEFAULT_NAMESPACE` | Namespace where Jobs are created if the request does not specify one. |
| `SCHEDULER_AUTH_TOKEN` | Bearer token required from the frontend. |
| `KUBERNETES_SERVICE_HOST` | Set automatically by Kubernetes. |
| `KUBERNETES_SERVICE_PORT` | Set automatically by Kubernetes. |

It also reads the standard ServiceAccount files:

```text
/var/run/secrets/kubernetes.io/serviceaccount/token
/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
```

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Health check, no auth required. |
| `POST` | `/jobs` | Create a Kubernetes Job from a YAML manifest. |
| `GET` | `/jobs/<name>?namespace=<namespace>` | Read a Job. |
| `DELETE` | `/jobs/<name>?namespace=<namespace>` | Delete a Job. |

All `/jobs` endpoints require:

```text
Authorization: Bearer <SCHEDULER_AUTH_TOKEN>
```

## Helm integration

The chart runs this image directly. It no longer mounts scheduler code from the `scheduler-app` ConfigMap.

```yaml
scheduler:
  image:
    repository: ymaqsoodbsc/openvre-kubernetes
    tag: "scheduler-1.0"
```
