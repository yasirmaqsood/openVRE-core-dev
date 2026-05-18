# Getting started with the OpenVRE Helm chart

You will install OpenVRE on a Kubernetes cluster using the `helm` command.

---

## 1. What is in this folder?

| Item | Purpose |
|------|--------|
| `Chart.yaml` | Chart name and version (metadata). |
| `values.yaml` | **Default** configuration. Do not edit only this for production; use a **separate override file** (see below). |
| `templates/` | Kubernetes manifests with placeholders; Helm fills them from `values.yaml` + your overrides. |
| `files/mongodb-init/` | Same Mongo seed JSON as `openVRE-core-dev/mongodb/init_documents` (used when `dashboardMongo.initDocuments.useBundledDefaults` is true). |
| `files/keycloak/` | `realm-sample.json` from `openVRE-core-dev` (used when `keycloak.realmImport.enabled` is true). |
| `README.md` | Reference for all options, architecture, packaging. |
| `sandbox-test.values.yaml` | **Example** overrides used for a test install (includes a sample `storageClassName`). Copy ideas from it, or use as a template. |


---

## 2. What you need before you start

1. **A Kubernetes cluster** you can use (cloud, on‑prem, or lab), version **1.25+**.
2. **`kubectl` installed** and configured to talk to that cluster:

   ```bash
   kubectl cluster-info
   kubectl get nodes
   ```

   If these fail, fix your kubeconfig or cluster access first.

3. **Helm 3.12+** installed:

   ```bash
   helm version
   ```

4. **An ingress controller** (for example **NGINX Ingress**) if you want to reach the app by a hostname. The chart defaults to `ingressClassName: nginx`. If you use another class, set `domain.ingressClassName` in your values file.

5. **Network access to pull container images** from the registries in `values.yaml` (or from **your** registry if you override `images.*`).

---

## 3. Understand Helm in one minute

- **Chart** = this folder (templates + default values).
- **Release** = one installed instance of the chart (has a name, e.g. `openvre`).
- **Namespace** = a logical partition in the cluster (e.g. `openvre-prod`).
- You install with: **`helm install <release-name> <path-to-chart> -f my-values.yaml -n <namespace> --create-namespace`**

Your **secrets and domain** should go in `my-values.yaml`, not committed to public git.

---

## 4. Step A — Fix storage (most common first‑time failure)

Persistent volumes need a **StorageClass** your cluster can provision.

On the cluster, run:

```bash
kubectl get storageclass
```

- If one row is marked **`(default)`**, you can leave `storageClassName` **empty** in the chart values (the chart omits the field and the cluster uses the default).
- If **nothing is default**, you **must** set `storageClassName` for every persistence block (Mongo, Postgres, frontend shared data, frontend tools), for example:

  ```yaml
  dashboardMongo:
    persistence:
      storageClassName: "local-path"   # example — use YOUR class name
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

See `sandbox-test.values.yaml` for a full small example.

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
   | `images.frontend` / `images.sgecore` | If you build your own images, point to **your** registry and tags. |

3. Optional: adjust `frontend.env.*` (Keycloak client name, scheduler URL is derived from the release namespace automatically).

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

**If you only want a quick test without DNS:**

Temporarily use port‑forward (works without Ingress DNS):

```bash
kubectl port-forward svc/dashboard-frontend 8080:80 -n openvre
```

Then browse **http://localhost:8080** .

After `helm install`, Helm prints **post‑install notes** (same content as `templates/NOTES.txt`) with port‑forward examples for Keycloak and MongoDB.

---

## 9. Step F — After install: what is automatic vs manual

### MongoDB (automatic with chart defaults)

On **first start with an empty Mongo data volume**, the chart runs the same pattern as `openVRE-core-dev` docker-compose: application user + `mongoimport` of bundled JSON under `files/mongodb-init/`. Controlled by **`dashboardMongo.initDocuments.enabled`** (default **true**) and **`useBundledDefaults`** (default **true**).

### Keycloak (manual by default)

**`keycloak.realmImport.enabled` defaults to `false`.** The bundled `realm-sample.json` is from a newer Keycloak dev export; automatic import against the chart’s default **Keycloak 15.x** image is **not reliable** (verification showed only the `master` realm in Postgres when import was enabled).

Create the OIDC client (default name **`openvre`**, realm **`master`** unless you change values), set **`secrets.frontend.keycloakSecret`**, then `helm upgrade` if needed. See `README.md`.

### Optional: try realm import (advanced)

Enable **`keycloak.realmImport.enabled: true`** only with a **realm JSON compatible with your Keycloak version**, or after upgrading the Keycloak image and startup command to match (e.g. Keycloak 26 + `--import-realm` as in `docker-compose`).

---

## 10. Upgrading and uninstalling

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

## 11. Packaging the chart to share as a single file

From the directory **containing** the chart folder:

```bash
helm package ./openvre-helm-chart
```

This produces something like `openvre-1.0.0.tgz`. Recipients install with:

```bash
helm install openvre ./openvre-1.0.0.tgz -f my-values.yaml -n openvre --create-namespace
```

They still need their own **`my-values.yaml`** (secrets, domain, storage class).

---

## 12. Where to read more

- **`README.md`** (in this folder) — full configuration table, TLS, hardening, architecture.
- **`helm show readme ./openvre-helm-chart`** — display README from the chart path.

If something fails, collect:

```bash
kubectl get events -n openvre --sort-by='.lastTimestamp' | tail -30
kubectl describe pod -n openvre <pod-name>
```

Those two outputs are usually enough to see scheduling, image pull, or PVC binding problems.
