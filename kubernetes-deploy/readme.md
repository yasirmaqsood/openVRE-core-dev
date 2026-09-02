# Getting started with the OpenVRE Helm chart

For a **full lab runbook** (empty cluster → dedicated project + shared pool, NFS, node taints, Keycloak hosts), see **[README-DEDICATED-AND-SHARED.md](./README-DEDICATED-AND-SHARED.md)**.

You will install OpenVRE on a Kubernetes cluster using the `helm` command.

---

## 1. What is in this folder?

| Item | Purpose |
|------|--------|
| `Chart.yaml` | Chart name and version (metadata). |
| `values.yaml` | **Default** configuration. Do not edit only this for production; use a **separate override file** (see below). |
| `templates/` | Kubernetes manifests with placeholders; Helm fills them from `values.yaml` + your overrides. |
| `files/mongodb-init/` | Mongo seed JSON (used when `dashboardMongo.initDocuments.useBundledDefaults` is true). |
| `files/keycloak/` | `realm-sample.json`(used when `keycloak.realmImport.enabled` is true). |



---

## 2. What you need before you start

1. **A Kubernetes cluster** - version **1.25+**.
2. **`kubectl` installed** and configured to talk to that cluster:

   ```bash
   kubectl cluster-info
   kubectl get nodes
   ```

   If these fail, fix your kubeconfig or cluster access first.

3. **Helm 3.12+** installed.

   If Helm is not installed, install it on your machine:

   ```bash
   curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
   ```

   Or install it with your OS package manager. For example, on Ubuntu/Debian with Snap:

   ```bash
   sudo snap install helm --classic
   ```

   Check:

   ```bash
   helm version
   ```

4. **An ingress controller** (for example **NGINX Ingress**) if you want to reach the app by a hostname. The chart defaults to `ingressClassName: nginx`. If you use another class, set `domain.ingressClassName` in your values file.

5. **Network access to pull container images** from the registries in `values.yaml`.

---

## 3. Understand Helm

- **Chart** = this folder (templates + default values).
- **Release** = one installed instance of the chart (has a name, e.g. `openvre`).
- **Namespace** = a logical partition in the cluster (e.g. `openvre-prod`).
- You install with: **`helm install <release-name> <path-to-chart> -f my-values.yaml -n <namespace> --create-namespace`**

Your **secrets and domain** should go in `my-values.yaml`, not committed to public git.

---

## 4. Step A — Config storage (If fresh cluster)

Persistent volumes need a **StorageClass** your cluster can provision.

On the cluster, run:

```bash
kubectl get storageclass
```

- If one row is marked **`(default)`**, you can leave `storageClassName` **empty** in the chart values (the chart omits the field and the cluster uses the default).
- If **nothing is default**, you **must** set `storageClassName` for every persistence block (Mongo, Postgres, frontend shared data, frontend tools)



### If this is a fresh cluster with no PVC provisioner

If `kubectl get storageclass` shows no usable StorageClass, install Rancher's **local-path-provisioner**. This is a simple storage provisioner . It stores PVC data on the node's local disk, so it is not a highly available storage backend.

Install it:

```bash
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/master/deploy/local-path-storage.yaml
```

Wait until it is running:

```bash
kubectl -n local-path-storage rollout status deploy/local-path-provisioner
kubectl get storageclass
```

You should see a StorageClass named `local-path`.

For this chart, the safest option is to explicitly set `storageClassName: "local-path"` in your `my-values.yaml`:

```yaml
dashboardMongo:
  persistence:
    storageClassName: "local-path"

postgres:
  persistence:
    storageClassName: "local-path"

frontend:
  sharedData:
    persistence:
      storageClassName: "local-path"
  tools:
    persistence:
      storageClassName: "local-path"
```

Optional: make `local-path` the cluster default StorageClass, so charts that omit `storageClassName` can use it automatically:

```bash
kubectl patch storageclass local-path \
  -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
```

Check:

```bash
kubectl get storageclass
```

You should now see `local-path (default)`.

---

## 5. Step B — Create your own values file

Do **not** rely on the default passwords in `values.yaml`.

1. Copy defaults to a new file:

   ```bash
   cd /path/to/openvre-helm-chart
   cp values.yaml my-values.yaml
   ```

2. Edit **`my-values.yaml`** and set at least:

   | Setting | Why |
   |--------|-----|
   | `domain.host` | Hostname for the Ingress (e.g. `openvre.company.com`). Must match how users open the site (and DNS must point to the ingress). |
   | `secrets.dashboardMongo.rootPassword` | MongoDB admin password. |
   | `secrets.dashboardMongo.appPassword` | MongoDB application user password. |
   | `secrets.keycloak.adminPassword` | Keycloak admin UI password. |
   | `secrets.keycloak.dbPassword` | PostgreSQL password used by Keycloak. |
   | `secrets.frontend.keycloakSecret` | OIDC client secret (see post‑install; you may create the client first, then put the secret here and `helm upgrade`). |
   | `scheduler.authToken` | Shared bearer token; frontend and scheduler must match (chart wires both from this value). |
   | `persistence.storageClassName` | As in Step A, if your cluster has no default StorageClass. |



Keep **`my-values.yaml` private** (passwords).

---

## 6. Step C — Validate before applying (recommended)

From the **parent directory** of the chart, or using the full path:

```bash
helm lint ./openvre-helm-chart
```

Render manifests locally (no cluster changes) to inspect YAML:

```bash
helm template openvre ./openvre-helm-chart -f ./openvre-helm-chart/my-values.yaml -n openvre > /tmp/openvre-rendered.yaml
```

Skim `/tmp/openvre-rendered.yaml` for obvious mistakes (wrong namespace in notes is OK; check image names and secrets references).

---

## 7. Step D — Install

Pick a **namespace** (e.g. `openvre`) and a **release name** (e.g. `openvre`):

```bash
helm install openvre ./openvre-helm-chart \
  -f ./openvre-helm-chart/my-values.yaml \
  -n openvre \
  --create-namespace \
  --wait \
  --timeout 15m
```

- `--create-namespace` creates the namespace if it does not exist.
- `--wait` waits until resources are ready (can fail on slow storage or wrong StorageClass).

Check:

```bash
helm status openvre -n openvre
kubectl get pods,pvc -n openvre
```

If **Pods stay Pending** and events mention **PersistentVolumeClaims**, return to Step A and set `storageClassName`, then delete stuck PVCs if Helm already created them wrong:

```bash
kubectl delete pvc --all -n openvre   # only if you are OK losing that namespace’s data
helm upgrade --install openvre ./openvre-helm-chart -f ./openvre-helm-chart/my-values.yaml -n openvre --wait --timeout 15m
```

---

## 8. Step E — How to open the application

**If you set `domain.host` and Ingress:**

1. Create a **DNS record** for `domain.host` pointing to your ingress controller’s external IP (or load balancer).
2. Open `http://` or `https://` + that host (depending on `domain.tlsEnabled` and your TLS secret).

### Local setup: install NGINX Ingress and access

Use this when Kubernetes is running inside a VM and you want to access OpenVRE from your device using a hostname.

This setup installs **ingress-nginx** as a **NodePort** service:

- HTTP on VM port `30080`
- HTTPS on VM port `30443`
- Ingress class name `nginx`

Install the controller:

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update

helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  -n ingress-nginx \
  --create-namespace \
  --set controller.service.type=NodePort \
  --set controller.service.nodePorts.http=30080 \
  --set controller.service.nodePorts.https=30443 \
  --set controller.ingressClassResource.name=nginx \
  --set controller.ingressClass=nginx \
  --wait
```

Check it:

```bash
kubectl get pods,svc -n ingress-nginx
kubectl get ingressclass
```

You should see an IngressClass named `nginx` and a Service exposing ports like `80:30080/TCP` and `443:30443/TCP`.

In your `my-values.yaml`, set the chart host and ingress class:

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

`domain.host` must stay as just the hostname because Kubernetes Ingress hosts cannot include a port. `keycloak.frontendUrl` includes `:30080` because this lab setup exposes ingress-nginx through a NodePort, and Keycloak redirects must exactly match the browser URL.

### Keycloak realm import (automatic OIDC setup)

The chart ships `files/keycloak/realm-sample.json` (same export as `openVRE-core-dev/keycloak/realms/realm-sample.json` in the openvre-dev-kubernetes repo). When `keycloak.realmImport.enabled` is `true` (default in `values.yaml`):

1. An init container patches the JSON (client secret, redirect URIs for `domain.host`, optional users).
2. Keycloak 26 starts with `--import-realm` and loads `project-realm.json` (or `<keycloak.realm>-realm.json`).

Align these values with the bundled export (simplest path):

| Value | Default |
|--------|---------|
| `keycloak.realm` | `project` |
| `keycloak.realmImport.oidcClientId` | `open-vre` |
| `frontend.env.keycloakClient` | `open-vre` |
| `secrets.frontend.keycloakSecret` | your client secret |

Default test user (when `keycloak.realmImport.users` is set): `vreuser` / `vreuser`.

**Re-import:** Keycloak stores realm state in PostgreSQL. To apply a fresh realm JSON on an existing install, delete the Postgres PVC for Keycloak (`dashboard-postgres` data) or use a new namespace, then `helm upgrade --install`. `keycloak.realmImport.importStrategy: OVERWRITE_EXISTING` updates an existing realm on startup when the import file is present.

To disable import and configure Keycloak manually, set `keycloak.realmImport.enabled: false`.

If OpenVRE is already installed, apply the values:

```bash
helm upgrade openvre ./openvre-helm-chart \
  -f ./openvre-helm-chart/my-values.yaml \
  -n openvre \
  --wait \
  --timeout 15m
```

Check that the chart created the Ingress:

```bash
kubectl get ingress -n openvre
kubectl describe ingress dashboard-frontend -n openvre
```


---

## 9. Upgrading and uninstalling

**Change configuration** (edit `my-values.yaml`, then):

```bash
helm upgrade openvre ./openvre-helm-chart -f ./openvre-helm-chart/my-values.yaml -n openvre --wait --timeout 15m
```

**Remove the release:**

```bash
helm uninstall openvre -n openvre
```

PVCs are often **retained** on purpose. To delete data volumes:

```bash
kubectl delete pvc --all -n openvre
```

---

## 10. Packaging the chart to share as a single file

From the directory **containing** the chart folder:

```bash
helm package ./openvre-helm-chart
```

This produces something like `openvre-1.0.0.tgz`. Can be installed with:

```bash
helm install openvre ./openvre-1.0.0.tgz -f my-values.yaml -n openvre --create-namespace
```


---
