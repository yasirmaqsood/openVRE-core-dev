# Interactive tools on Kubernetes (path-based URLs)

This tree extends **bsc-tre-copy** with automatic interactive sessions (e.g. RStudio) launched from the OpenVRE UI.

Original trees are unchanged:

- `k8s-deployments/bsc-tre-copy`
- `openvre-dev-kubernetes`

## URL pattern

After a user launches an interactive tool on site **local** (`job_manager: kubernetes_native`):

```text
http://<your-public-ip>/<sanitized-user-id>/<session-id>/
```

Example:

```text
http://212.128.227.180/BSC_TRE_john_doe/rstudio-a1b2c3d4/
```

Each launch creates:

- Deployment + Service + ConfigMap (`rserver.conf` with `www-root-path`)
- Ingress path on **ingress-nginx** (same IP as OpenVRE)

Apache **does not** proxy interactive traffic when `interactive.disableLegacyApacheProxy: true`.

## What changed

| Component | Location |
|-----------|----------|
| Scheduler API `POST /interactive-sessions` | `scheduler/app.py` |
| PHP session launcher | `openvre-dev-kubernetes-interactive/.../ProcessK8sInteractive.php` |
| Tool launch / workspace link | `Tooljob.php`, `processJob.inc.php`, `projects.inc.php`, `actions-home.js` |
| Helm RBAC + values | `openvre-helm-chart/templates/scheduler.yaml`, `values.yaml` |
| RStudio tool files | `openvre-helm-chart/files/tools/rstudio/` |

## Build and deploy

### 1. Scheduler image

```bash
cd /home/ubuntu/k8s-deployments/bsc-tre-interactive/scheduler
docker build -t ymaqsoodbsc/openvre-kubernetes:scheduler-1.1-interactive .
docker push ymaqsoodbsc/openvre-kubernetes:scheduler-1.1-interactive
```

### 2. Frontend image (includes PHP/JS patches)

```bash
cd /home/ubuntu/openvre-dev-kubernetes-interactive/openVRE-core-dev/front_end
# Use your existing frontend build/push pipeline, e.g.:
docker build -t ymaqsoodbsc/openvre-kubernetes:frontend-2.1-interactive .
docker push ymaqsoodbsc/openvre-kubernetes:frontend-2.1-interactive
```

### 3. Helm upgrade

```bash
cd /home/ubuntu/k8s-deployments/bsc-tre-interactive/openvre-helm-chart
# my-values.yaml: domain, secrets, scheduler.authToken, images tags

helm upgrade bsc-tre-v2 . -f my-values.yaml -n bsctre-v2 \
  --set images.frontend.tag=frontend-2.1-interactive \
  --set scheduler.image.tag=scheduler-1.1-interactive
```

### 4. MongoDB — site + tool

Site **local** must have:

```javascript
db.sites.updateOne(
  { _id: "local" },
  { $set: { "launcher.job_manager": "kubernetes_native" } }
)
```

Register RStudio tool (see `files/tools/rstudio/mongo-k8s.json`):

```bash
# paste JSON from mongo-k8s.json into mongosh
db.tools.replaceOne({ _id: "rstudio" }, <document>, { upsert: true })
```

Copy tool UI into the frontend tools PVC (or bake into image):

```bash
kubectl cp files/tools/rstudio/. <ns>/<frontend-pod>:/var/www/html/openVRE/public/tools/rstudio/
```

## User flow

1. User opens tool **Rstudio Session** in OpenVRE.
2. Frontend calls scheduler `POST /interactive-sessions`.
3. Job appears in workspace as **ACTIVE SESSION**.
4. **Access Session** opens `/<user-id>/<session-id>/` on the same host (e.g. `212.128.227.180`).
5. **Stop Session** calls `DELETE /interactive-sessions/{name}`.

## Auth (later)

Session URLs are reachable without OpenVRE login today. Options for later:

- ingress-nginx `auth-url` / OAuth2 proxy
- NetworkPolicy + internal-only Services
- Short-lived signed tokens in query string

## Quick test without full image rebuild

On an existing cluster, patch scheduler ConfigMap and RBAC:

```bash
kubectl apply -f - <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: scheduler
  namespace: bsctre-v2
rules:
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "get", "list", "delete"]
  - apiGroups: [""]
    resources: ["pods", "services", "configmaps"]
    verbs: ["create", "get", "list", "delete"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["create", "get", "list", "delete"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses"]
    verbs: ["create", "get", "list", "delete"]
EOF

kubectl create configmap scheduler-app \
  --from-file=app.py=/home/ubuntu/k8s-deployments/bsc-tre-interactive/scheduler/app.py \
  -n bsctre-v2 --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deployment/scheduler -n bsctre-v2
```

Test API:

```bash
TOKEN=$(kubectl get secret scheduler-auth -n bsctre-v2 -o jsonpath='{.data.token}' | base64 -d)
curl -s -X POST http://scheduler.bsctre-v2.svc.cluster.local:8080/interactive-sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"bsctre-v2","user_id":"testuser","session_id":"rstudio-demo1","image":"rocker/rstudio:4.4.2"}'
```

Then open: `http://212.128.227.180/testuser/rstudio-demo1/` (password `openvre`).
