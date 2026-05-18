# Kubernetes deployment (`kubernetes-interactive-pod-with-auth` branch)

Matches OpenVRE auth variant: gateway + interactiveAuth + ingress auth-url.

- `scheduler/app.py` — interactive sessions + optional ingress auth annotations
- `openvre-helm-chart/` — set `interactive.openvreAuth.enabled: true`

```bash
helm upgrade --install openvre ./kubernetes-deploy/openvre-helm-chart \
  -f ./kubernetes-deploy/openvre-helm-chart/values.yaml -n YOUR_NS
```
