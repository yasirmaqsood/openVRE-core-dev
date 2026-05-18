# Getting started with the OpenVRE Helm chart

**Branch:** `kubernetes-with-deploy` — full Helm deploy bundle with **batch** Kubernetes launcher (no interactive-session PHP on this branch).

**Working directory:**

```bash
cd kubernetes-deploy/openvre-helm-chart   # from repository root
```

For interactive pods, use branches `kubernetes-interactive-pod` or `kubernetes-interactive-pod-with-auth`.
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

### If this is a fresh cluster with no PVC provisioner

If `kubectl get storageclass` shows no usable StorageClass, install Rancher's **local-path-provisioner**. This is a simple storage provisioner for single-node clusters, lab clusters, k3s/RKE-style environments, or quick tests. It stores PVC data on the node's local disk, so it is not a highly available storage backend.

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
   cd kubernetes-deploy/openvre-helm-chart  # from repo root
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
helm lint .
```

Render manifests locally (no cluster changes) to inspect YAML:

```bash
helm template openvre . -f ./my-values.yaml -n openvre > /tmp/openvre-rendered.yaml
```

Skim `/tmp/openvre-rendered.yaml` for obvious mistakes (wrong namespace in notes is OK; check image names and secrets references).

---

## 7. Step D — Install

Pick a **namespace** (e.g. `openvre`) and a **release name** (e.g. `openvre`):

```bash
helm install openvre . \
  -f ./my-values.yaml \
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
helm upgrade --install openvre ./openvre-helm-chart -f ./my-values.yaml -n openvre --wait --timeout 15m
```

---

## 8. Step E — How to open the application

**If you set `domain.host` and Ingress:**

1. Create a **DNS record** for `domain.host` pointing to your ingress controller’s external IP (or load balancer).
2. Open `http://` or `https://` + that host (depending on `domain.tlsEnabled` and your TLS secret).

### VM/lab setup: install NGINX Ingress and open from your laptop

Use this when Kubernetes is running inside a VM and you want to access OpenVRE from your laptop using a hostname, without `kubectl port-forward`.

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

If OpenVRE is already installed, apply the values:

```bash
helm upgrade openvre . \
  -f ./my-values.yaml \
  -n openvre \
  --wait \
  --timeout 15m
```

Check that the chart created the Ingress:

```bash
kubectl get ingress -n openvre
kubectl describe ingress dashboard-frontend -n openvre
```

Find the VM IP:

```bash
hostname -I
```

On your **laptop**, add a hosts entry pointing the hostname to the VM IP.

Linux/macOS:

```bash
sudo sh -c 'echo "<vm-ip> openvre.local" >> /etc/hosts'
```

Windows, edit this file as Administrator:

```text
C:\Windows\System32\drivers\etc\hosts
```

Add:

```text
<vm-ip> openvre.local
```

Now open from your laptop:

```text
http://openvre.local:30080
```

If it does not load, check that the VM network mode/firewall allows your laptop to reach port `30080`. For VirtualBox/VMware NAT, you may need port forwarding from laptop port `30080` to VM port `30080`; bridged networking is usually simpler for this lab setup.

**If you only want a quick test without DNS:**

Temporarily use port‑forward (works without Ingress DNS):

```bash
kubectl port-forward svc/dashboard-frontend 8080:80 -n openvre
```

Then browse **http://localhost:8080** .

### Access from your laptop when Kubernetes is inside a VM

If the cluster is running inside a VM on your laptop and you did **not** install an Ingress controller, the frontend Service is still only reachable inside the cluster. Use one of these options.

**Option 1: `kubectl` works on your laptop**

If your laptop's `kubectl` can talk to the VM cluster, run this on your laptop:

```bash
kubectl port-forward svc/dashboard-frontend 8080:80 -n openvre
```

Then open:

```text
http://localhost:8080
```

**Option 2: `kubectl` only works inside the VM**

Open one terminal on your laptop and SSH into the VM:

```bash
ssh <vm-user>@<vm-ip>
```

Inside the VM, run:

```bash
kubectl port-forward svc/dashboard-frontend 8080:80 -n openvre
```

Then, from another terminal on your laptop, create an SSH tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 <vm-user>@<vm-ip>
```

Now open this on your laptop:

```text
http://localhost:8080
```

**Option 3: expose the port-forward on the VM IP**

Inside the VM:

```bash
kubectl port-forward --address 0.0.0.0 svc/dashboard-frontend 8080:80 -n openvre
```

Then open this from your laptop:

```text
http://<vm-ip>:8080
```

Use this only on a trusted local network. Your VM firewall, VirtualBox/VMware network mode, or laptop hypervisor NAT settings may also need to allow port `8080`.

To find the VM IP:

```bash
ip addr
```

or:

```bash
hostname -I
```

After `helm install`, Helm prints **post‑install notes** (same content as `templates/NOTES.txt`) with port‑forward examples for Keycloak and MongoDB.

---

## 9. Step F — After install: what is automatic vs manual

### MongoDB (automatic with chart defaults)

On **first start with an empty Mongo data volume**, the chart runs the same pattern as `openVRE-core-dev` docker-compose: application user + `mongoimport` of bundled JSON under `files/mongodb-init/`. Controlled by **`dashboardMongo.initDocuments.enabled`** (default **true**) and **`useBundledDefaults`** (default **true**).

### Keycloak (manual by default)

**`keycloak.realmImport.enabled` defaults to `false`.** The bundled `realm-sample.json` is from a newer Keycloak dev export; automatic import against the chart’s default **Keycloak 15.x** image is **not reliable** (verification showed only the `master` realm in Postgres when import was enabled).

Create the OIDC client (default name **`openvre`**, realm **`master`** unless you change values), set **`secrets.frontend.keycloakSecret`**, then `helm upgrade` if needed. See `README.md`.

### Create the Keycloak client and test user

Use these steps after the pods are running and you can open OpenVRE through Ingress or port-forward.

The chart defaults are:

```yaml
keycloak:
  realm: master
  frontendUrl: "http://openvre.local:30080/auth"   # for the VM/NodePort example

frontend:
  env:
    keycloakClient: openvre
```

If Keycloak shows an **invalid parameter** page while opening the admin console, check this first: the browser URL and `keycloak.frontendUrl` must match. For the VM/NodePort example, use:

```yaml
keycloak:
  frontendUrl: "http://openvre.local:30080/auth"
```

Then run:

```bash
helm upgrade openvre . \
  -f ./my-values.yaml \
  -n openvre \
  --wait \
  --timeout 15m
```

Open the Keycloak admin console. If you followed the VM/Ingress example above:

```text
http://openvre.local:30080/auth/admin
```

If you are using port-forward instead:

```bash
kubectl port-forward svc/dashboard-frontend 8080:80 -n openvre
```

Then open:

```text
http://localhost:8080/auth/admin
```

Log in with the Keycloak admin credentials from `my-values.yaml`:

```yaml
secrets:
  keycloak:
    adminUser: admin
    adminPassword: <your-keycloak-admin-password>
```

In Keycloak, select the `master` realm, then create the OpenVRE client:

1. Go to **Clients**.
2. Click **Create**.
3. Set **Client ID** to `openvre`.
4. Set **Client Protocol** to `openid-connect`.
5. Save.
6. Set **Access Type** to `confidential`.
7. Turn **Standard Flow Enabled** on.
8. Set **Valid Redirect URIs** to match the URL you use in the browser:

   ```text
   http://openvre.local:30080/*
   ```

   For port-forward testing, use:

   ```text
   http://localhost:8080/*
   ```

9. Set **Web Origins** to:

   ```text
   http://openvre.local:30080
   ```

   For port-forward testing:

   ```text
   http://localhost:8080
   ```

10. Save.

Then copy the client secret:

1. Open the `openvre` client.
2. Go to the **Credentials** tab.
3. Copy the **Secret** value.
4. Put it in `my-values.yaml`:

   ```yaml
   secrets:
     frontend:
       keycloakSecret: "<copied-openvre-client-secret>"
   ```

Apply the updated secret to the frontend:

```bash
helm upgrade openvre . \
  -f ./my-values.yaml \
  -n openvre \
  --wait \
  --timeout 15m
```

Create a test user:

1. In Keycloak, go to **Users**.
2. Click **Add user**.
3. Set **Username** and turn **Enabled** on.
4. Save.
5. Open the **Credentials** tab.
6. Set a password.
7. Turn **Temporary** off for simple testing.
8. Save.

Now open OpenVRE and try logging in:

```text
http://openvre.local:30080
```

If login redirects fail, the most common cause is that **Valid Redirect URIs** does not exactly match the browser URL you are using, including the port.

### Create the Keycloak client and user with commands

You can do the same setup from the Keycloak pod using `kcadm.sh`.

Enter the Keycloak pod:

```bash
kubectl -n openvre exec -it deploy/dashboard-keycloak -- bash
```

Inside the pod, log in to Keycloak admin:

```bash
KC=/opt/jboss/keycloak/bin/kcadm.sh

$KC config credentials \
  --server http://127.0.0.1:8080/auth \
  --realm master \
  --user "$KEYCLOAK_USER" \
  --password "$KEYCLOAK_PASSWORD"
```

Create or update the OpenVRE client. For the VM/NodePort example, the browser URL is `http://openvre.local:30080`, so the redirect URI and web origin must include `:30080`:

```bash
CLIENT_ID=$($KC get clients -r master -q clientId=openvre --fields id --format csv | tail -n 1 | tr -d '"')

if [ -z "$CLIENT_ID" ]; then
  CLIENT_ID=$($KC create clients -r master -i \
    -s clientId=openvre \
    -s enabled=true \
    -s protocol=openid-connect \
    -s publicClient=false \
    -s bearerOnly=false \
    -s standardFlowEnabled=true \
    -s directAccessGrantsEnabled=true \
    -s 'redirectUris=["http://openvre.local:30080/*"]' \
    -s 'webOrigins=["http://openvre.local:30080"]')
else
  $KC update clients/$CLIENT_ID -r master \
    -s enabled=true \
    -s protocol=openid-connect \
    -s publicClient=false \
    -s bearerOnly=false \
    -s standardFlowEnabled=true \
    -s directAccessGrantsEnabled=true \
    -s 'redirectUris=["http://openvre.local:30080/*"]' \
    -s 'webOrigins=["http://openvre.local:30080"]'
fi
```

Print the client secret:

```bash
$KC get clients/$CLIENT_ID/client-secret -r master
```

Copy the `"value"` from that output into `my-values.yaml`:

```yaml
secrets:
  frontend:
    keycloakSecret: "<copied-openvre-client-secret>"
```

Create a test user. OpenVRE expects Keycloak to return an `email` claim; without it, login can redirect back to the frontend but still show **Login** instead of the user name.

```bash
$KC create users -r master \
  -s username=testuser \
  -s enabled=true \
  -s email=testuser@example.com \
  -s firstName=Test \
  -s lastName=User \
  -s emailVerified=true

$KC set-password -r master \
  --username testuser \
  --new-password 'TestPassword123!' \
  --temporary=false
```

Exit the Keycloak pod, then apply the updated frontend client secret:

```bash
helm upgrade openvre . \
  -f ./my-values.yaml \
  -n openvre \
  --wait \
  --timeout 15m
```

Restart the frontend so it reloads the Secret from its environment:

```bash
kubectl -n openvre rollout restart deployment/dashboard-frontend
kubectl -n openvre rollout status deployment/dashboard-frontend
```

Then log in with:

```text
username: testuser
password: TestPassword123!
```

If you see `unauthorized_client` after login, the frontend is usually using the wrong or old client secret. Re-copy the `openvre` client secret from Keycloak, update `secrets.frontend.keycloakSecret`, run `helm upgrade`, and restart `deployment/dashboard-frontend` again.

If login redirects back to OpenVRE but the top bar still shows **Login**, check that the Keycloak user has `email`, `firstName`, `lastName`, and `emailVerified=true`. For an existing user, fix it with:

```bash
USER_ID=$($KC get users -r master -q username=testuser --fields id --format csv | tail -n 1 | tr -d '"')

$KC update users/$USER_ID -r master \
  -s enabled=true \
  -s email=testuser@example.com \
  -s emailVerified=true \
  -s firstName=Test \
  -s lastName=User
```

Then clear browser cookies for `openvre.local`, or use a private browser window, and try logging in again.

### Switch the local site launcher to Kubernetes native

For testing tool execution through Kubernetes Jobs, update the `local` site document in MongoDB so OpenVRE uses the `kubernetes_native` launcher.

Enter the MongoDB pod:

```bash
kubectl -n openvre exec -it deploy/dashboard-mongodb -- sh
```

Open `mongosh` using the MongoDB app user:

```bash
mongosh --host 127.0.0.1 --port 27017 \
  -u "$MONGO_INITDB_USERNAME" \
  -p "$MONGO_INITDB_PASSWORD" \
  --authenticationDatabase "$MONGO_MAIN_DB" \
  "$MONGO_MAIN_DB"
```

Run:

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

Verify:

```javascript
db.sites.findOne({ _id: "local" }, { launcher: 1 })
```

Each tool also needs a valid container image for Kubernetes Jobs. For example:

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

For Kubernetes-native tool Jobs, CPU and memory come from the tool document in MongoDB (`infrastructure.cpus` and `infrastructure.memory`). On a small VM cluster, start with `1` CPU and `1Gi` memory. If a tool pod stays Pending with `Insufficient cpu` or `Insufficient memory`, lower these values or add capacity to the Kubernetes node.

### Optional: try realm import (advanced)

Enable **`keycloak.realmImport.enabled: true`** only with a **realm JSON compatible with your Keycloak version**, or after upgrading the Keycloak image and startup command to match (e.g. Keycloak 26 + `--import-realm` as in `docker-compose`).

---

## 10. Upgrading and uninstalling

**Change configuration** (edit `my-values.yaml`, then):

```bash
helm upgrade openvre . -f ./my-values.yaml -n openvre --wait --timeout 15m
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
helm package .
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

---

## 13. Interactive tools (other branches)

This branch’s `scheduler/app.py` supports **batch Jobs only**. For RStudio per-session pods, switch to:

- **`kubernetes-interactive-pod`** — [GETTING_STARTED on that branch](https://github.com/yasirmaqsood/openVRE-core-dev/blob/kubernetes-interactive-pod/kubernetes-deploy/openvre-helm-chart/GETTING_STARTED.md) + [INTERACTIVE.md](https://github.com/yasirmaqsood/openVRE-core-dev/blob/kubernetes-interactive-pod/kubernetes-deploy/INTERACTIVE.md)
- **`kubernetes-interactive-pod-with-auth`** — OpenVRE-gated session URLs

The base install in Sections 2–9 above is still required for any variant.
