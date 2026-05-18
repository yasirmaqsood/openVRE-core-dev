# Kubernetes deployment (base `kubernetes` branch)

Batch jobs via scheduler + OpenVRE Helm chart. No per-session interactive pods.

- `scheduler/` — Job create/monitor/delete only
- `openvre-helm-chart/` — full stack Helm chart
- `interactive-poc/` — reference manifests (not used by base frontend)
- `cluster-bundle/` — rendered manifest bundle

```bash
helm upgrade --install openvre ./kubernetes-deploy/openvre-helm-chart -n YOUR_NS
```
