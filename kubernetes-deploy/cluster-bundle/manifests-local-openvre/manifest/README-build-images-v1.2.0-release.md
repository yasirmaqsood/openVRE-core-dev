# Build and Deploy Local Images (openVRE v1.2.0_release)

This guide documents exactly what was done to build and deploy the custom images now used by `bsctre-v2`:

- `local/openvre-frontend:v1.2.0-release`
- `local/openvre-sgecore:v1.2.0-release`

Source used:
- openVRE repo: [inab/openVRE v1.2.0_release](https://github.com/inab/openVRE/tree/v1.2.0_release)
- checked-out commit: `3e81ac64532a2fbb4bc438f4adf6ca2a9f0a8968`

---

## 1) Clone the exact release source

```bash
git clone --branch v1.2.0_release --single-branch https://github.com/inab/openVRE.git /home/ubuntu/openVRE
git -C /home/ubuntu/openVRE rev-parse HEAD
```

Why:
- Locks builds to a known release line and exact commit for reproducibility.

---

## 2) Frontend Dockerfile fixes required for current environment

File changed:
- `/home/ubuntu/openVRE/front_end/Dockerfile`

### 2.1 MongoDB GPG URL update

Changed:
- `https://www.mongodb.org/static/pgp/server-7.0.asc`
to
- `https://pgp.mongodb.com/server-7.0.asc`

Why:
- The old URL intermittently fails or is deprecated; the new URL is the working upstream endpoint.

### 2.2 Disable Composer network steps during image build

Removed build-time steps:
- `composer self-update`
- `composer update ...`

Why:
- In this environment, Composer downloads were timing out during Docker build (`curl`/SSL timeout).
- Build reliability improved by pre-seeding dependencies instead of fetching during build.

---

## 3) Pre-seed frontend `vendor/` before building

```bash
cp -a /home/ubuntu/openVRE-core-dev/front_end/openVRE/vendor /home/ubuntu/openVRE/front_end/openVRE/
```

Why:
- The image still needs PHP dependencies present.
- Copying a known-good `vendor/` allows deterministic, offline-friendly builds.

Note:
- This is a pragmatic CI/stability workaround. The preferred long-term approach is a locked `composer.lock` + reproducible dependency mirror/cache.

---

## 4) Build images

### 4.1 Frontend

```bash
sudo docker build -t local/openvre-frontend:v1.2.0-release /home/ubuntu/openVRE/front_end
```

### 4.2 SGE core

```bash
sudo docker build \
  --build-arg DOCKER_GROUP=1002 \
  --build-arg SUBMITTER_HOSTNAME=dashboard-frontend.bsctre-v2.svc.cluster.local \
  -t local/openvre-sgecore:v1.2.0-release \
  /home/ubuntu/openVRE/sge
```

Why these SGE args:
- `DOCKER_GROUP=1002`: avoids group ID mismatch issues in the image.
- `SUBMITTER_HOSTNAME=...`: writes scheduler submit host config expected by current cluster service naming.

---

## 5) Import images into Kubernetes container runtime (containerd)

```bash
sudo docker save local/openvre-frontend:v1.2.0-release -o /home/ubuntu/openvre-frontend-v1.2.0-release.tar
sudo docker save local/openvre-sgecore:v1.2.0-release -o /home/ubuntu/openvre-sgecore-v1.2.0-release.tar

sudo ctr -n k8s.io images import /home/ubuntu/openvre-frontend-v1.2.0-release.tar
sudo ctr -n k8s.io images import /home/ubuntu/openvre-sgecore-v1.2.0-release.tar
```

Why:
- Kubernetes nodes pull from `containerd`, not the local Docker image store.
- Importing ensures pods can start from `local/*` images without external registry pulls.

---

## 6) Point Helm values to new tags

File:
- `/home/ubuntu/k8s-deployments/bsc-tre/helm/bsc-tre/values.yaml`

Set:
- `images.frontend.repository: local/openvre-frontend`
- `images.frontend.tag: v1.2.0-release`
- `images.sgecore.repository: local/openvre-sgecore`
- `images.sgecore.tag: v1.2.0-release`

Why:
- Ensures deployment references the newly built local release images.

---

## 7) Deploy with Helm

```bash
helm upgrade --install bsc-tre-v2 /home/ubuntu/k8s-deployments/bsc-tre/helm/bsc-tre \
  -n bsctre-v2 \
  -f /home/ubuntu/k8s-deployments/bsc-tre/helm/bsc-tre/values.yaml \
  --wait --timeout 15m
```

---

## 8) Verify rollout and image usage

```bash
kubectl get deploy -n bsctre-v2 dashboard-frontend dashboard-sgecore \
  -o jsonpath='{range .items[*]}{.metadata.name}{" => "}{.spec.template.spec.containers[0].image}{" | avail="}{.status.availableReplicas}{"/"}{.status.replicas}{"\n"}{end}'

kubectl rollout status -n bsctre-v2 deployment/dashboard-frontend --timeout=180s
kubectl rollout status -n bsctre-v2 deployment/dashboard-sgecore --timeout=180s
```

Expected:
- frontend: `local/openvre-frontend:v1.2.0-release`
- sgecore: `local/openvre-sgecore:v1.2.0-release`
- both deployments available.

---

## 9) Refresh reproducible manifest bundle

After Helm/value changes, regenerate:

```bash
helm template bsc-tre-v2 /home/ubuntu/k8s-deployments/bsc-tre/helm/bsc-tre \
  -n bsctre-v2 \
  -f /home/ubuntu/k8s-deployments/bsc-tre/helm/bsc-tre/values.yaml \
  > /home/ubuntu/k8s-deployments/bsc-tre/cluster-bundle/manifests-local-openvre/_all.yaml
```

Then split into per-resource files (already done in this folder).

---

## Current operational notes

- Frontend pod security context includes `fsGroup: 1000` to keep service-account token readable by app user and avoid `Kubernetes in-cluster API/token not available`.
- If dependency/network conditions improve, replace vendor pre-seeding with strict Composer lockfile-based installs in build.
