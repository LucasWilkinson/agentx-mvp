#!/usr/bin/env bash
# List vllm-envs (`ve`) environments on the workspace PVC via any running pod that mounts it.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE
pvc="${WORKSPACE_PVC:-workspace-lwilkinson}"
pod="$(k get pods --field-selector=status.phase=Running \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{range .spec.volumes[*]}{.persistentVolumeClaim.claimName}{" "}{end}{"\n"}{end}' \
  | awk -v pvc="$pvc" '{for(i=2;i<=NF;i++) if($i==pvc){print $1; exit}}')"
[[ -n "$pod" ]] || { echo "ERROR: no running pod mounts $pvc (start the devbox: ../devbox.sh up)" >&2; exit 1; }
k exec -i "$pod" -- python3 - <<'PY'
import json, subprocess
envs = json.load(open("/workspace/.cache/vllm-envs/envs.json"))
for name, e in envs.items():
    p = e["path"]
    try:
        head = subprocess.check_output(["git", "-C", p, "log", "-1", "--format=%h %ad %s", "--date=short"], text=True).strip()
    except Exception:
        head = "?"
    print(f"{name:16} {p:40} {head}")
PY
echo
echo "Use: VLLM_ENV=<path> just deploy [spec]    (new env: on the devbox, 've new <ref> --name <name>')"
