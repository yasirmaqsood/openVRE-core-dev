#!/usr/bin/env bash
# Install OpenStack Cinder CSI (once per cluster).
# Run on a host with kubectl (the master) and Helm 3.
#
# Usage:
#   bash cinder-csi/install-cinder-csi.sh /path/to/app-cred-cinder-csi-openvre-openrc.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OPENRC="${1:-}"

if [[ -z "${OPENRC}" ]]; then
  echo "Usage: $0 /path/to/app-cred-*-openrc.sh" >&2
  exit 1
fi
if [[ ! -f "${OPENRC}" ]]; then
  echo "OpenRC file not found: ${OPENRC}" >&2
  exit 1
fi
if ! command -v kubectl >/dev/null; then
  echo "kubectl not found. Run this on the Kubernetes master." >&2
  exit 1
fi
if ! command -v helm >/dev/null; then
  echo "helm not found." >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a
source "${OPENRC}"
set +a

: "${OS_AUTH_URL:?OS_AUTH_URL missing in openrc}"
: "${OS_APPLICATION_CREDENTIAL_ID:?OS_APPLICATION_CREDENTIAL_ID missing}"
: "${OS_APPLICATION_CREDENTIAL_SECRET:?OS_APPLICATION_CREDENTIAL_SECRET missing}"
: "${OS_REGION_NAME:=RegionOne}"

TMP_CONF="$(mktemp)"
trap 'rm -f "${TMP_CONF}"' EXIT

cat > "${TMP_CONF}" <<EOF
[Global]
auth-url=${OS_AUTH_URL}
application-credential-id=${OS_APPLICATION_CREDENTIAL_ID}
application-credential-secret=${OS_APPLICATION_CREDENTIAL_SECRET}
region=${OS_REGION_NAME}

[BlockStorage]
ignore-volume-az=true
EOF

kubectl create namespace kube-system --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic cloud-config \
  -n kube-system \
  --from-file=cloud.conf="${TMP_CONF}" \
  --dry-run=client -o yaml | kubectl apply -f -

helm repo add cpo https://kubernetes.github.io/cloud-provider-openstack
helm repo update cpo

helm upgrade --install cinder-csi cpo/openstack-cinder-csi \
  --namespace kube-system \
  -f "${ROOT}/values-cinder-csi.yaml" \
  --wait --timeout 10m

kubectl apply -f "${ROOT}/storageclass-cinder-csi.yaml"

echo
echo "Cinder CSI installed. Check:"
echo "  kubectl -n kube-system get pods | grep -i cinder"
echo "  kubectl get storageclass cinder-csi"
echo
echo "Optional PVC test (delete afterwards):"
echo "  kubectl apply -f ${ROOT}/test-pvc.yaml"
echo "  kubectl get pvc,pv -n default"
