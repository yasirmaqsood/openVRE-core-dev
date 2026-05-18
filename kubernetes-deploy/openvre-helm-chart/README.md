# OpenVRE Helm Chart

Deploy the [OpenVRE](https://openvre.eu/) Virtual Research Environment on any
Kubernetes cluster.

If you are new to Helm or this chart, start with **[GETTING_STARTED.md](./GETTING_STARTED.md)**. It includes the full verified lab flow: fresh-cluster storage, ingress-nginx in a VM, Keycloak client/user setup, Mongo launcher switch, and Kubernetes-native tool execution.

## Prerequisites

| Requirement | Minimum version |
|---|---|
| Kubernetes cluster | 1.25+ |
| Helm | 3.12+ |
| Ingress controller (e.g. NGINX) | any |
| Default StorageClass **or** explicit `storageClassName` | — |

## Quick start

```bash
# 1. Create a values override file
cp values.yaml my-values.yaml
# Edit my-values.yaml — at minimum set:
#   domain.host, all secrets.*, images.frontend, images.sgecore

# 2. Install
helm install openvre ./openvre-helm-chart \
  -f my-values.yaml \
  -n openvre \
  --create-namespace

# 3. Check status
helm status openvre -n openvre
kubectl get pods -n openvre
```

## Configuration

All settings live in `values.yaml`. Below are the most important ones.

### Required settings (you MUST change these)

| Key | Description |
|---|---|
| `domain.host` | Your domain name (e.g. `openvre.example.com`; use only the hostname, no port) |
| `keycloak.frontendUrl` | Public Keycloak URL if it differs from `http://<domain.host>/auth`, e.g. `http://openvre.local:30080/auth` for NodePort labs |
| `secrets.dashboardMongo.rootPassword` | MongoDB root password |
| `secrets.dashboardMongo.appPassword` | MongoDB application password |
| `secrets.frontend.keycloakSecret` | Keycloak OIDC client secret |
| `secrets.keycloak.adminPassword` | Keycloak admin password |
| `secrets.keycloak.dbPassword` | PostgreSQL password for Keycloak |
| `scheduler.authToken` | Shared token between frontend and scheduler |

### Images

| Key | Default | Description |
|---|---|---|
| `images.frontend.repository` | `ymaqsoodbsc/openvre-kubernetes` | Frontend Docker image |
| `images.frontend.tag` | `frontend-2.0` | Frontend image tag |
| `images.sgecore.repository` | `ymaqsoodbsc/openvre-kubernetes` | SGE core Docker image |
| `images.sgecore.tag` | `sgecore-1.0` | SGE core image tag |
| `images.keycloak.repository` | `quay.io/keycloak/keycloak` | Keycloak image |
| `images.keycloak.tag` | `15.0.2` | Keycloak image tag |
| `images.dashboardMongo.repository` | `mongo` | MongoDB image |
| `images.dashboardMongo.tag` | `8.0` | MongoDB image tag |
| `postgres.image.repository` | `postgres` | PostgreSQL image |
| `postgres.image.tag` | `17.3` | PostgreSQL image tag |
| `scheduler.image.repository` | `ymaqsoodbsc/openvre-kubernetes` | Scheduler image |
| `scheduler.image.tag` | `scheduler-1.0` | Scheduler image tag |

### Storage

By default the chart uses the cluster's **default StorageClass**. To use a
specific one, set `storageClassName` under the relevant persistence section:

```yaml
dashboardMongo:
  persistence:
    storageClassName: "my-storage-class"
postgres:
  persistence:
    storageClassName: "my-storage-class"
frontend:
  sharedData:
    persistence:
      storageClassName: "my-storage-class"
  tools:
    persistence:
      storageClassName: "my-storage-class"
```

For a fresh VM/lab cluster with no provisioner, `GETTING_STARTED.md` shows how to install Rancher's `local-path-provisioner` and set all four persistence blocks to `storageClassName: "local-path"`.

### VM / NodePort ingress

For a single VM on your laptop, the verified lab setup uses `ingress-nginx` as a NodePort service:

```yaml
domain:
  host: "openvre.local"
  ingressClassName: nginx
  tlsEnabled: false

keycloak:
  frontendUrl: "http://openvre.local:30080/auth"

frontend:
  ingress:
    enabled: true
```

Add `openvre.local` to your laptop hosts file pointing to the VM IP, then open:

```text
http://openvre.local:30080
```

`domain.host` must not include the port because Kubernetes Ingress host rules only accept hostnames. `keycloak.frontendUrl` includes the port so Keycloak redirects match the URL used by the browser.

### Component toggles

Disable any component you don't need:

```yaml
components:
  frontend: { enabled: true }
  keycloak: { enabled: true }
  dashboardMongo: { enabled: true }
  postgres: { enabled: true }
  sgecore: { enabled: true }
```

### TLS / HTTPS

```yaml
domain:
  tlsEnabled: true
  tlsSecretName: "openvre-tls"
```

The TLS secret must exist in the target namespace (e.g. created by
cert-manager).

### Hardening

Network policies and pod disruption budgets are **enabled by default**.
Disable them for development clusters:

```yaml
hardening:
  podDisruptionBudgets:
    enabled: false
  networkPolicies:
    enabled: false
```

## Architecture

```
                   Ingress
                     │
                     ▼
               ┌───────────┐
               │  Frontend  │──▶ Scheduler ──▶ K8s API (creates Jobs)
               └─────┬─────┘
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
      Keycloak    MongoDB    SGE Core
          │
          ▼
      PostgreSQL
```

| Component | Purpose |
|---|---|
| **Frontend** | PHP web application (Apache) serving the OpenVRE UI |
| **Scheduler** | Lightweight Python HTTP proxy that creates/monitors/deletes K8s Jobs on behalf of the frontend |
| **Keycloak** | Single Sign-On (OIDC) identity provider |
| **PostgreSQL** | Keycloak's backend database |
| **MongoDB** | Application database (tools, files, users) |
| **SGE Core** | Legacy grid engine bridge for job submission |

## Post-install setup

### MongoDB (default: automatic)

With **`dashboardMongo.initDocuments.enabled: true`** and **`useBundledDefaults: true`** (defaults), the same JSON seed as `openVRE-core-dev` is loaded on **first start with an empty Mongo volume**.

### Keycloak (default: manual client)

**`keycloak.realmImport.enabled` defaults to `false`.** The bundled `files/keycloak/realm-sample.json` is from a newer Keycloak dev export; **`KEYCLOAK_IMPORT`** on the chart’s default **Keycloak 15.x** image did **not** create the `project` realm in cluster verification (only `master` existed in Postgres). Enable import only with a **realm JSON compatible with your Keycloak image**, or upgrade Keycloak and startup args (e.g. Keycloak 26 + `--import-realm` as in `docker-compose`).

Create the OIDC client matching **`frontend.env.keycloakClient`** (default **`openvre`**) in realm **`master`** (or set `keycloak.realm`), set **`secrets.frontend.keycloakSecret`**, and `helm upgrade` if you change the secret after install.

### Users

Create at least one user in the realm your frontend uses. For OpenVRE login to complete, the Keycloak user must have an email claim. The tested setup uses:

```text
username: testuser
email: testuser@example.com
firstName: Test
lastName: User
emailVerified: true
```

See `GETTING_STARTED.md` for both UI and `kcadm.sh` commands. If login redirects back to OpenVRE but the top bar still shows **Login**, check the user's `email`, `firstName`, `lastName`, and `emailVerified` fields.

### Kubernetes-native tool execution

The chart includes a lightweight `scheduler` Deployment. The frontend submits tool Jobs to this scheduler, and the scheduler creates Kubernetes `batch/v1` Jobs using its ServiceAccount.

To make the seeded `local` site use Kubernetes Jobs, update MongoDB:

```javascript
db.sites.updateOne(
  { _id: "local" },
  {
    $set: {
      "launcher.job_manager": "kubernetes_native",
      "launcher.container": "Kubernetes"
    }
  }
)
```

Each tool also needs a valid container image and reasonable resources in its Mongo document:

```javascript
db.tools.updateOne(
  { _id: "tool_skeleton" },
  {
    $set: {
      "infrastructure.clouds.local.launcher": "kubernetes_native",
      "infrastructure.clouds.local.queue": "kubernetes",
      "infrastructure.container_image": "ymaqsoodbsc/fem-tool-2-updated:1.0",
      "infrastructure.cpus": 1,
      "infrastructure.memory": 1
    }
  }
)
```

For Kubernetes-native Jobs, CPU and memory come from MongoDB tool metadata (`infrastructure.cpus` and `infrastructure.memory`), not from `scheduler.resources` in `values.yaml`. On small VM clusters, start with `cpus: 1` and `memory: 1` to avoid `Insufficient cpu` / `Insufficient memory` scheduling errors.

## Upgrading

```bash
helm upgrade openvre ./openvre-helm-chart -f my-values.yaml -n openvre
```

## Uninstalling

```bash
helm uninstall openvre -n openvre
```

> **Note:** PersistentVolumeClaims are not deleted automatically. Remove them
> manually if you want to reclaim storage:
> `kubectl delete pvc --all -n openvre`

## Packaging and distributing

```bash
# Package into a .tgz archive
helm package ./openvre-helm-chart

# Share the resulting openvre-1.0.0.tgz file, or host it in a Helm repository
```
