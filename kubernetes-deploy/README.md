# Kubernetes deploy bundle — `kubernetes-interactive-pod-with-auth`

Same as the pods branch, plus **OpenVRE ingress auth** and **gateway** for session URLs.

## Cluster prerequisites (read first)

OpenVRE on Kubernetes **will not work reliably** without these steps. Full detail is in **[openvre-helm-chart/GETTING_STARTED.md](./openvre-helm-chart/GETTING_STARTED.md)** (from [bsc-tre-copy](https://github.com/yasirmaqsood/openVRE-core-dev) production guide):

| Step | GETTING_STARTED section | What you must have |
|------|-------------------------|-------------------|
| Tools | **§2** | Kubernetes **1.25+**, `kubectl`, **Helm 3.12+**, image pull access |
| Storage | **§4** | A **StorageClass** on every PVC (Mongo, Postgres, shared data, tools) — or install **local-path-provisioner** |
| Ingress | **§8** | **NGINX Ingress** (NodePort `30080` lab setup or cloud load balancer) + DNS/`/etc/hosts` for `domain.host` |
| Secrets | **§5** | Private **`my-values.yaml`**: Mongo/Keycloak passwords, `scheduler.authToken`, `domain.host` |
| Install | **§7** | `helm install` / `upgrade` with `--wait` |
| Post-install | **§9** | Keycloak **openvre** client + secret, test user, Mongo site **`kubernetes_native`** |
| Batch tools | **§9** (end) | Tool documents with `container_image`, CPU/memory for scheduler Jobs |

Optional: **§6** lint/template before apply · **§10** upgrade/uninstall · **§11** package chart.

**Do not skip storage or ingress** — most first-time failures are Pending PVCs or unreachable `domain.host`.

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
