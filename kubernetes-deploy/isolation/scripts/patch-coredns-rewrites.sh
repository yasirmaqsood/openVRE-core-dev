#!/usr/bin/env bash
# Add CoreDNS rewrite rules so in-cluster pods can resolve project hostnames
# to the shared ingress-nginx controller (needed for Keycloak OIDC token exchange).
#
# Usage:
#   ./patch-coredns-rewrites.sh proja.openvre.local projb.openvre.local projc.openvre.local
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <hostname> [hostname...]" >&2
  exit 1
fi

TARGET="ingress-nginx-controller.ingress-nginx.svc.cluster.local"
tmpjson="$(mktemp)"
trap 'rm -f "$tmpjson"' EXIT

python3 - "$TARGET" "$tmpjson" "$@" <<'PY'
import json, subprocess, sys

target = sys.argv[1]
out_path = sys.argv[2]
hosts = sys.argv[3:]

raw = subprocess.check_output(
    ["kubectl", "-n", "kube-system", "get", "cm", "coredns", "-o", "json"],
    text=True,
)
obj = json.loads(raw)
core = obj["data"]["Corefile"]
changed = False

for host in hosts:
    needle = f"rewrite name {host} {target}"
    if needle in core:
        print(f"skip (exists): {host}", file=sys.stderr)
        continue
    line = f"        {needle}\n"
    if "rewrite name " in core:
        lines = core.splitlines(True)
        out = []
        last_rw = -1
        for ln in lines:
            out.append(ln)
            if "rewrite name " in ln:
                last_rw = len(out) - 1
        if last_rw >= 0:
            out.insert(last_rw + 1, line)
            core = "".join(out)
        else:
            core = core.replace("kubernetes ", line + "        kubernetes ", 1)
    else:
        core = core.replace("kubernetes ", line + "        kubernetes ", 1)
    print(f"add: {host} -> {target}", file=sys.stderr)
    changed = True

if not changed:
    print("No CoreDNS changes needed.", file=sys.stderr)
    open(out_path, "w").write("")
    sys.exit(0)

obj["data"]["Corefile"] = core
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
    obj.get("metadata", {}).pop(k, None)
open(out_path, "w").write(json.dumps(obj))
PY

if [[ -s "$tmpjson" ]]; then
  kubectl apply -f "$tmpjson"
  kubectl -n kube-system rollout restart deploy/coredns
  kubectl -n kube-system rollout status deploy/coredns --timeout=120s
  echo "CoreDNS updated and restarted."
fi
