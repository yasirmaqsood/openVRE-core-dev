# OpenVRE — branch `kubernetes-with-deploy`

Backup / full-deploy branch: **batch Kubernetes** OpenVRE PHP (same as `kubernetes`) **plus** the complete `kubernetes-deploy/` bundle (Helm, scheduler, cluster-bundle).

Use this branch to reproduce a full stack install without interactive-session PHP.

## Start here

| Doc | Purpose |
|-----|---------|
| [kubernetes-deploy/openvre-helm-chart/GETTING_STARTED.md](./kubernetes-deploy/openvre-helm-chart/GETTING_STARTED.md) | Install from scratch |
| [kubernetes-deploy/openvre-helm-chart/README.md](./kubernetes-deploy/openvre-helm-chart/README.md) | Chart reference |
| [kubernetes-deploy/README.md](./kubernetes-deploy/README.md) | Deploy bundle layout |
| [kubernetes-deploy/scheduler/README.md](./kubernetes-deploy/scheduler/README.md) | Batch Job scheduler only |

## Interactive variants

For RStudio per-session pods, use:

- **`kubernetes-interactive-pod`**
- **`kubernetes-interactive-pod-with-auth`**

Those branches include interactive PHP and `kubernetes-deploy/INTERACTIVE.md`.
