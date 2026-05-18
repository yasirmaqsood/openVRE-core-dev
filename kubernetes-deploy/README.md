# Kubernetes deploy bundle — `kubernetes-with-deploy`

Full Helm + scheduler bundle for **batch** OpenVRE on Kubernetes (no interactive-session PHP on this branch).

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
