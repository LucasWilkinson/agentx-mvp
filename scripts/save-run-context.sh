#!/usr/bin/env bash
# Usage: save-run-context.sh <config_dir> [pod-selector]
# Per-config context beside the results: prefill/decode YAML, router YAML (EPP, its config, InferencePool),
# a chart label, and — when a selector is given and pods are live — `kubectl describe` per pod and namespace events.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE
dir="${1:?usage: save-run-context.sh <config_dir> [pod-selector]}"; selector="${2:-}"
helm --kube-context "$KUBE_CONTEXT" -n "$NAMESPACE" get manifest "$ROUTER_RELEASE" 2>/dev/null \
  | uv run --quiet --project "$MANIFESTO_ROOT" python scripts/run-context.py "$dir"
if [[ -n "$selector" ]]; then
  mkdir -p "$dir/logs"
  for pod in $(k get pods -l "$selector" -o jsonpath='{.items[*].metadata.name}'); do
    k describe pod "$pod" > "$dir/logs/${pod}.describe" 2>&1 || true
  done
  k get events --sort-by=.lastTimestamp > "$dir/logs/events.txt" 2>&1 || true
fi
