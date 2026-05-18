# Production Hardening Plan (BSC TRE / OpenVRE)

This document explains how to move the current cluster to production-grade security posture and what has already been applied.

## What was applied now (safe hardening pass)

Applied in Helm templates:
- `keycloak.yaml`
- `dashboard-mongo.yaml`
- `postgres.yaml`
- `sgecore.yaml`

Changes:
- `automountServiceAccountToken: false` for workloads that do not need Kubernetes API access.
- Pod-level seccomp:
  - `securityContext.seccompProfile.type: RuntimeDefault`
- Container hardening for Keycloak, MongoDB, PostgreSQL:
  - `runAsNonRoot: true`
  - `allowPrivilegeEscalation: false`
  - `capabilities.drop: ["ALL"]`

Why:
- Reduces API token exposure surface.
- Uses kernel default syscall profile.
- Enforces least-privilege execution model.

## Why frontend/sgecore are not fully non-root yet

Current runtime behavior still requires root-like operations:

- Frontend container startup command copies override PHP files into application directories and starts services on low ports through base entrypoint.
- SGE core startup runs:
  - `groupmod -g $DOCKER_GROUP docker`
  - `usermod -aG docker application`
  which are privileged account-management operations.

To make these truly non-root in production, we need image and startup refactors.

## Required next phase (image-level hardening)

### 1) Frontend image/runtime refactor
- Pre-bake patched files (`users.inc.php`, `ProcessK8s.php`, etc.) into image at build time or mount read-only overrides.
- Remove runtime root copy operations from container command.
- Run web process as non-root (e.g., `runAsUser: 1000`, `runAsGroup: 1000`, `runAsNonRoot: true`).
- If port 80 binding requires privileges, move app to high port (e.g., 8080) and keep Service targetPort mapping.

### 2) SGE image/runtime refactor
- Move `groupmod/usermod` to image build stage, not startup.
- Ensure runtime user/group are pre-created and stable.
- Run container as non-root in deployment spec.

### 3) Frontend API permissions
- Keep dedicated service account only for frontend if it needs in-cluster Job creation.
- Tighten Role to minimum verbs/resources actually used (already namespace-scoped Role; validate exact verbs).

### 4) Additional production controls
- Add NetworkPolicies:
  - allow only required namespace/service flows (frontend -> keycloak/mongo/sgecore, keycloak -> postgres, etc.)
- Add PodDisruptionBudgets for critical services.
- Add readiness/liveness probes everywhere.
- Add resource requests/limits validation for all workloads.
- Move secrets out of plain Helm values into external secret manager (Vault/ExternalSecrets/SealedSecrets).
- Enable image vulnerability scanning and signed images in CI.

## Suggested rollout sequence

1. Apply the current safe hardening pass (already done in templates).
2. Refactor frontend image/startup to non-root.
3. Refactor sgecore image/startup to non-root.
4. Enable strict Pod Security Admission baseline/restricted for namespace.
5. Add NetworkPolicy and validate tool execution paths.
6. Add CI policy checks (kube-linter, Trivy, Kyverno/Gatekeeper policies).

## Validation checklist after each phase

- `kubectl get pods -n bsctre-v2`
- `kubectl rollout status` for all deployments
- Login flow works in frontend
- Tool launch works (`kubernetes_native`)
- No pod runs as root:
  - `kubectl get pod -n bsctre-v2 -o jsonpath=...` (inspect effective `runAsUser`, `runAsNonRoot`)
