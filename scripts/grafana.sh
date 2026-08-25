#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh
load_agentx_env
require_agentx_env KUBE_CONTEXT NAMESPACE

port="${GRAFANA_LOCAL_PORT:-3000}"
echo "Grafana: http://127.0.0.1:${port}/d/wideep-overview"
echo "Stop with Ctrl-C."
k port-forward service/grafana "${port}:80"

