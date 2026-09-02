#!/usr/bin/env bash
# Install project-b on the shared node pool with split userdata/admindata NFS.
set -euo pipefail
CHART="$(cd "$(dirname "$0")/.." && pwd)"
cd "$CHART"

kubectl apply -f nfs/k8s-nfs-pvs.yaml

helm upgrade --install openvre "$CHART" \
  -n project-b --create-namespace \
  -f my-values.yaml \
  -f values-shared-pool.yaml \
  -f values-project-b.yaml \
  -f values-project-b-storage.yaml \
  --timeout 15m --wait

if [[ -d isolation/scripts ]]; then
  bash isolation/scripts/apply-isolation.sh project-b
  bash isolation/scripts/patch-coredns-rewrites.sh projb.openvre.local
fi

bash scripts/seed-rstudio-tool-files.sh project-b || true

echo "Done. Verify: kubectl -n project-b get pods -o wide"
