#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh
load_agentx_env
require_agentx_env KUBE_CONTEXT NAMESPACE

latest="$(k get jobs \
  -l app.kubernetes.io/name=agentx-aiperf --sort-by=.metadata.creationTimestamp \
  -o name | tail -1)"
[[ -n "$latest" ]] || { echo "ERROR: no AgentX Jobs found" >&2; exit 2; }
k logs -f "$latest"

