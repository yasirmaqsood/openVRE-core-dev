# Interactive tools PoC (RStudio on bsctre-v2)

Bypasses the OpenVRE Apache `interactive-tool` proxy. Each session is a **Deployment + Service + Ingress** on **ingress-nginx**.

## Deployed on cluster

```bash
kubectl apply -f rstudio-sessions.yaml
```

## Access (VM NodePort 30080)

Add to **your laptop** `/etc/hosts` (use your VM IP):

```
192.168.0.226  rstudio-1.openvre.local rstudio-2.openvre.local
```

| Session | URL | Login |
|---------|-----|--------|
| 1 | http://rstudio-1.openvre.local:30080/ | user `rstudio`, password `openvre` |
| 2 | http://rstudio-2.openvre.local:30080/ | same |

## OpenVRE tool registration

- UI files: `/var/www/html/openVRE/public/tools/rstudio/` (copied from `from-fedcomp-to-local/tools/rstudio`)
- Mongo: `db.tools` document `_id: rstudio` (see `rstudio-tool-mongo.json`)

The tool appears in the portal; **launching a session from the UI still uses the old docker_SGE path** until the scheduler/frontend are extended to create these Deployments automatically.

## Teardown

```bash
kubectl delete -f rstudio-sessions.yaml
kubectl exec -n bsctre-v2 deploy/dashboard-mongodb -- mongosh \
  "mongodb://mongo_root_user:dev-mongo-root-password@localhost:27017/db?authSource=admin" \
  --eval 'db.tools.deleteOne({_id:"rstudio"})'
```

## Next step (production)

Extend the scheduler with `POST /interactive-sessions` to create Deployment + Service + Ingress per job, using either:

- **Per-session hostname** (this PoC), or
- **Path prefix** `/interactive-tool/<id>/` if ingress snippets or RStudio `www-root-path` are configured
