# Kubernetes deploy bundle — `kubernetes-with-deploy`

Full Helm + scheduler bundle for **batch** OpenVRE on Kubernetes (no interactive-session PHP on this branch).

## Layout

| Path | Description |
|------|-------------|
| [openvre-helm-chart/](./openvre-helm-chart/) | Full stack — [GETTING_STARTED.md](./openvre-helm-chart/GETTING_STARTED.md) |
| [scheduler/](./scheduler/) | Batch `Job` API only (~133 lines in `app.py`) |
| [interactive-poc/](./interactive-poc/) | Reference only (not wired to this branch’s frontend) |
| [cluster-bundle/](./cluster-bundle/) | Pre-rendered manifests |

## Quick install

```bash
cd kubernetes-deploy/openvre-helm-chart
helm upgrade --install openvre . -f my-values.yaml -n YOUR_NS --create-namespace
```

For interactive pods, switch to branch **`kubernetes-interactive-pod`** or **`kubernetes-interactive-pod-with-auth`**.
