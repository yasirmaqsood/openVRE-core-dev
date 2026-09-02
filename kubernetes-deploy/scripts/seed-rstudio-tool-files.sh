#!/usr/bin/env bash
# Copy bundled RStudio tool UI into the frontend tools PVC.
#
# Layout (upstream OpenVRE tool structure):
#   public/tools/rstudio/front/...
# URL:
#   http://openvre.local/tools/rstudio/front/input.php?op=0
#
# Usage: ./scripts/seed-rstudio-tool-files.sh [namespace]
set -euo pipefail

NS="${1:-openvre}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="/var/www/html/openVRE/public/tools/rstudio/front"

POD="$(kubectl -n "$NS" get pod -l app=dashboard-frontend -o jsonpath='{.items[0].metadata.name}')"
echo "Using frontend pod: $POD"

kubectl -n "$NS" exec "$POD" -- mkdir -p "$DEST"
kubectl -n "$NS" cp "$ROOT/files/tools/rstudio/." "$POD:$DEST/"
kubectl -n "$NS" exec "$POD" -- test -f "$DEST/input.php"

echo "Done. Seeded $DEST"
