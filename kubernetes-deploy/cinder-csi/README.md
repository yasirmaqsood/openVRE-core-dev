# Cinder CSI (Mongo / Postgres)

Install **once** on the cluster. Helm storage overlays already set `storageClassName: cinder-csi` for Mongo and Postgres. Userdata/admindata stay on NFS. Tools stay on `local-path`.

## Install (on the master)

```bash
cd kubernetes-deploy
chmod +x cinder-csi/install-cinder-csi.sh
bash cinder-csi/install-cinder-csi.sh /path/to/app-cred-cinder-csi-openvre-openrc.sh
```

The script:

1. Builds `cloud.conf` from the OpenRC application credential (not committed)
2. Creates Secret `kube-system/cloud-config`
3. Helm-installs `cpo/openstack-cinder-csi`
4. Applies StorageClass `cinder-csi` (`WaitForFirstConsumer`, `Retain`)

Do **not** copy the OpenRC / clouds.yaml into this git tree.

## Check

```bash
kubectl -n kube-system get pods | grep -i cinder
kubectl get storageclass cinder-csi
```

Optional smoke test (creates a 1Gi OpenStack volume):

```bash
kubectl apply -f cinder-csi/test-pvc.yaml
kubectl get pvc,pod cinder-csi-smoke
# then
kubectl delete -f cinder-csi/test-pvc.yaml
openstack volume list   # confirm the leftover volume if Retain kept it
```

With `Retain`, deleting the PVC may leave a 1Gi volume in OpenStack — delete that test volume by hand.

## Rotate credentials later

Edit Secret `kube-system/cloud-config` (key `cloud.conf`) and restart:

```bash
kubectl -n kube-system rollout restart deploy -l app=openstack-cinder-csi
kubectl -n kube-system rollout restart ds -l app=openstack-cinder-csi
```

(Exact labels: `kubectl -n kube-system get deploy,ds | grep cinder`.)
