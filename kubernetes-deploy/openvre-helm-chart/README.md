# OpenVRE Helm Chart

Deploy the [OpenVRE](https://openvre.eu/) Virtual Research Environment on any
Kubernetes cluster.

If you are new to Helm or this chart, start with **[GETTING_STARTED.md](./GETTING_STARTED.md)** (step-by-step install, storage, and first checks).

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
| `domain.host` | Your domain name (e.g. `openvre.example.com`) |
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
| `images.frontend.tag` | `frontend-1.0` | Frontend image tag |
| `images.sgecore.repository` | `ymaqsoodbsc/openvre-kubernetes` | SGE core Docker image |
| `images.sgecore.tag` | `sgecore-1.0` | SGE core image tag |
| `images.keycloak.repository` | `quay.io/keycloak/keycloak` | Keycloak image |
| `images.keycloak.tag` | `15.0.2` | Keycloak image tag |
| `images.dashboardMongo.repository` | `mongo` | MongoDB image |
| `images.dashboardMongo.tag` | `8.0` | MongoDB image tag |
| `postgres.image.repository` | `postgres` | PostgreSQL image |
| `postgres.image.tag` | `17.3` | PostgreSQL image tag |

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

Create at least one user in the realm your frontend uses.

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
