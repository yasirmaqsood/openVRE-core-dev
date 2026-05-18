# Kubernetes deploy bundle — `kubernetes-interactive-pod-with-auth`

Same as the pods branch, plus **OpenVRE ingress auth** and **gateway** for session URLs.

## Layout

| Path | Description |
|------|-------------|
| [openvre-helm-chart/](./openvre-helm-chart/) | Helm chart — [GETTING_STARTED.md](./openvre-helm-chart/GETTING_STARTED.md) |
| [scheduler/](./scheduler/) | Scheduler with optional `OPENVRE_INTERACTIVE_AUTH_*` |
| [interactive-poc/](./interactive-poc/) | Manual RStudio PoC |
| [cluster-bundle/](./cluster-bundle/) | Rendered manifests (reference) |
| [INTERACTIVE.md](./INTERACTIVE.md) | Interactive + auth for **this variant** |

## Quick install

```bash
cd kubernetes-deploy/openvre-helm-chart
helm upgrade --install openvre . -f my-values.yaml -n YOUR_NS --create-namespace
```

Set `interactive.openvreAuth.enabled: true` and configure `authUrl` / `signInUrl` for your namespace and public host.

## OpenVRE frontend

This branch adds `front_end/openVRE/public/applib/interactive*.php` — required for auth and gateway.

See [INTERACTIVE.md](./INTERACTIVE.md).
