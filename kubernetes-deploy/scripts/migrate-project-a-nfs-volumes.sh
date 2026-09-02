#!/usr/bin/env bash
# Rebind project-a userdata/admindata to new split NFS PVs (mongo/postgres/tools unchanged).
set -euo pipefail
CHART="$(cd "$(dirname "$0")/.." && pwd)"
NS=project-a

kubectl apply -f "$CHART/nfs/k8s-nfs-pvs.yaml"

echo "Scaling down project-a workloads..."
kubectl -n "$NS" scale deploy --all --replicas=0
sleep 10

for pvc in dashboard-frontend-sgecore-shareddata openvre-admindata; do
  echo "Recreating PVC $pvc"
  kubectl -n "$NS" delete pvc "$pvc" --ignore-not-found --wait=true
done

cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: dashboard-frontend-sgecore-shareddata
  namespace: ${NS}
spec:
  storageClassName: project-a-userdata-nfs
  accessModes: ["ReadWriteMany"]
  resources:
    requests:
      storage: 25Gi
  volumeName: pv-proj-a-userdata
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: openvre-admindata
  namespace: ${NS}
spec:
  storageClassName: project-a-admindata-nfs
  accessModes: ["ReadWriteMany"]
  resources:
    requests:
      storage: 25Gi
  volumeName: pv-proj-a-admindata
EOF

helm upgrade openvre "$CHART" \
  -n "$NS" \
  -f "$CHART/my-values.yaml" \
  -f "$CHART/values-project-a.yaml" \
  -f "$CHART/values-project-a-storage.yaml" \
  --timeout 15m --wait

echo "project-a migrated to split NFS volumes."
