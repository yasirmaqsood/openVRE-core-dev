# BSC-TRE Helm — interactive pods + OpenVRE auth

**`interactive.openvreAuth.enabled: true`** — ingress `auth-url` / `auth-signin`, RStudio `DISABLE_AUTH` when auth is on.

**OpenVRE frontend:** `openvre-dev-kubernetes-interactive-auth`

Auth endpoints must be reachable from ingress-nginx (see `values.yaml` `authUrl` / `signInUrl`).
