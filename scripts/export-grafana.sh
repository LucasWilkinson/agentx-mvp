#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh
load_agentx_env
require_agentx_env KUBE_CONTEXT NAMESPACE GRAFANA_ADMIN_USER GRAFANA_ADMIN_PASSWORD

port="${GRAFANA_EXPORT_PORT:-33001}"
# A previous export's port-forward can linger for a moment after it is killed; free the port first.
for _ in $(seq 1 20); do
  pids="$(lsof -ti "tcp:${port}" 2>/dev/null || true)"
  [[ -z "$pids" ]] && break
  kill $pids 2>/dev/null || true; sleep 0.5
done
k port-forward service/grafana "${port}:80" \
  >/tmp/glm52-agentx-grafana-forward.log 2>&1 &
forward_pid=$!
trap 'kill "$forward_pid" >/dev/null 2>&1 || true; wait "$forward_pid" 2>/dev/null || true' EXIT
sleep 2
kill -0 "$forward_pid" 2>/dev/null || {
  cat /tmp/glm52-agentx-grafana-forward.log >&2
  exit 1
}

directories="$(find results -name profile_export_aiperf.json \
  -not -path '*/.dashboard-input/*' -exec dirname {} \; | sort -u)"
[[ -n "$directories" ]] || { echo "ERROR: no downloaded results" >&2; exit 2; }

# Pod scoping comes from each run's sibling config_name.txt / pods.txt written by scripts/sweep.sh.
python3 export_dashboard.py \
  --grafana-url "http://127.0.0.1:${port}" \
  --auth "${GRAFANA_ADMIN_USER}:${GRAFANA_ADMIN_PASSWORD}" \
  --deployment '.*' \
  results $directories
