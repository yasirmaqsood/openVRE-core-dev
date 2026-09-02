#!/usr/bin/env bash
# Apply OpenVRE namespace isolation pack to one or more namespaces.
#
# Usage:
#   ./apply-isolation.sh project-a
#   ./apply-isolation.sh project-a project-b project-c
#
# Prerequisites:
#   - kubectl configured for the target cluster
#   - namespaces may already exist (Helm --create-namespace) or will be updated/created
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="$ROOT/base"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <namespace> [namespace...]" >&2
  exit 1
fi

# Cluster-scoped policy once
kubectl apply -f "$BASE/validating-admission-policy.yaml"

render() {
  local ns="$1"
  local src="$2"
  sed "s/__NAMESPACE__/${ns}/g" "$src"
}

for ns in "$@"; do
  echo "=== Applying isolation to namespace: ${ns} ==="
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN

  for f in \
    namespace-psa.yaml \
    resourcequota-limitrange.yaml \
    rbac.yaml \
    networkpolicy-extra.yaml \
    validating-admission-policy-binding.yaml \
    isolation-status-configmap.yaml
  do
    render "$ns" "$BASE/$f" > "$tmp/$f"
    kubectl apply -f "$tmp/$f"
  done

  echo "Done: ${ns}"
  echo
done

echo "Verify with:"
echo "  kubectl get ns -L openvre.project,pod-security.kubernetes.io/enforce"
echo "  kubectl -n <ns> get resourcequota,limitrange,networkpolicy,role"
echo "  kubectl get validatingadmissionpolicybinding | grep openvre"
