#!/usr/bin/env bash
set -u

cd "$(dirname "$0")/.."
ENV_FILE=${ENV_FILE:-.env.b200}
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
: "${KUBECONFIG:?set KUBECONFIG in the ignored $ENV_FILE}"
: "${KUBE_CONTEXT:?set KUBE_CONTEXT in the ignored $ENV_FILE}"
: "${NAMESPACE:?set NAMESPACE in the ignored $ENV_FILE}"
interval=${MONITOR_INTERVAL_SECONDS:-600}
artifact_root=${ARTIFACT_ROOT:-results/.artifacts/lmsys-glm-agentic-b200}
log=${MONITOR_LOG:-$artifact_root/monitor.log}
mkdir -p "$(dirname "$log")"

while :; do
  {
    printf '\n=== %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    screen -ls 2>&1 | grep -E 'glm53-(pcp|a2a)-(client|router)' || \
      echo 'WARNING: one or more benchmark screen sessions may be absent'
    kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get pods -l 'llm-d.ai/role in (prefill,decode)' \
      -o custom-columns='POD:.metadata.name,READY:.status.containerStatuses[*].ready,PHASE:.status.phase,RESTARTS:.status.containerStatuses[*].restartCount' \
      2>&1 | grep -E 'POD|glm53-(pcp8|tp8)'
    find "$artifact_root/pcp8-ep8" \
      "$artifact_root/pcp8-dcp8-ep8-a2a" \
      -maxdepth 2 -type f -name 'benchmark_data.db*' -print 2>/dev/null | sort
    for client_log in \
      "$artifact_root/pcp8-ep8-client.log" \
      "$artifact_root/pcp8-dcp8-ep8-a2a-client.log"; do
      printf '%s: ' "$client_log"
      tr '\r' '\n' < "$client_log" 2>/dev/null \
        | grep 'Processing\[parallel_' | tail -1 || echo 'no progress line yet'
    done
  } >> "$log" 2>&1
  sleep "$interval"
done
