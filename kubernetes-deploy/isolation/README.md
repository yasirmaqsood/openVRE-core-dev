# Isolation pack — file reference

Manifests live in `base/`. The apply script substitutes `__NAMESPACE__` and
`kubectl apply`s them.

The **fresh-cluster deploy runbook** (dedicated + shared pool, split NFS, Helm `-f` files) is:

`../README-DEDICATED-AND-SHARED.md`

## Files

| File | Scope | What it does |
|------|--------|----------------|
| `validating-admission-policy.yaml` | Cluster | Denies pods with privileged, hostPath, hostNetwork, hostPID, hostIPC |
| `validating-admission-policy-binding.yaml` | Per project | Binds the policy to namespaces labeled `openvre.project=<ns>` |
| `namespace-psa.yaml` | Namespace | Labels for PSA baseline enforce + restricted warn/audit; sets `openvre.project` |
| `resourcequota-limitrange.yaml` | Namespace | Caps pods/CPU/memory/PVC; default container requests/limits |
| `rbac.yaml` | Namespace | `project-admin` / `project-viewer` Roles; disables default SA token automount |
| `networkpolicy-extra.yaml` | Namespace | Ingress only from same namespace or `ingress-nginx` |
| `isolation-status-configmap.yaml` | Namespace | Marker ConfigMap documenting pack version |
| `node-pinning-example.yaml` | Example | Legacy **local** PV pin. Current lab uses NFS PVs in `../nfs/k8s-nfs-pvs.yaml` |

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/apply-isolation.sh` | Apply pack to one or more namespaces |
| `scripts/patch-coredns-rewrites.sh` | Rewrite project hostnames → ingress-nginx Service |
| `scripts/mount-project-volume-on-node.sh` | Legacy: format/mount a disk at `/data/<project>` (not used for current NFS) |
| `examples/install-three-projects.sh` | Helm project-a (dedicated) + project-b (shared) + isolation + DNS + seed |

## Helm values used for a fresh deploy

| File | Role |
|------|------|
| `../my-values.yaml` | Secrets, images (shared) |
| `../values-project-a.yaml` | Dedicated pin + `proja.openvre.local` + Keycloak URL |
| `../values-project-a-storage.yaml` | project-a NFS userdata/admindata; mongo/postgres = cinder-csi; tools = local-path |
| `../cinder-csi/` | OpenStack Cinder CSI install (once) |
| `../values-shared-pool.yaml` | Pin to `openvre.pool=shared` |
| `../values-project-b.yaml` | Host `projb.openvre.local` + Keycloak URL |
| `../values-project-b-storage.yaml` | project-b NFS classes; mongo/postgres = cinder-csi |
| `../nfs/setup-nfs-four-volumes.sh` | Format/mount/export four Cinder volumes on the NFS server |
| `../nfs/k8s-nfs-pvs.yaml` | StorageClasses + static NFS PVs |
| `../scripts/install-project-b-shared.sh` | Wrapper for project-b only |
| `../scripts/seed-rstudio-tool-files.sh` | Copy RStudio UI into the tools PVC |
| `../.helmignore` | Exclude large PNGs so the Helm release Secret stays under 1 MiB |

## Relationship to Helm chart

Already in Helm (`hardening.networkPolicies.enabled`):

- `default-deny-all`
- DNS egress
- frontend / keycloak / mongo / postgres / scheduler / sgecore / interactive allows
- Dedicated ServiceAccounts for frontend & scheduler
- Scheduler namespace-scoped Role

Isolation pack **adds** PSA labels, quotas, extra ingress NP, human Roles, and VAP.

## Apply order (recommended)

1. Cluster prep + NFS + PVs (see `README-DEDICATED-AND-SHARED.md`)
2. `helm upgrade --install …` with the `-f` files above
3. `apply-isolation.sh <ns>`
4. `patch-coredns-rewrites.sh <hosts…>`
5. `seed-rstudio-tool-files.sh <ns>`
6. Confirm `dashboard-postgres` replicas = 1

## Binding humans to RBAC (optional)

```bash
kubectl -n project-a create rolebinding alice-admin \
  --role=project-admin \
  --user=alice@example.com
```

(Or use Groups from your OIDC/Kubernetes auth setup.)

## Done / remaining / loopholes

Full status tables live in the **top-level** README:

`openvre-core-dev-original-updated-with-policies/README.md`

→ section **“Status: done / remaining / loopholes”**.
