#!/usr/bin/env bash
# Usage: teardown-model.sh [spec|--all]   Delete manifesto-rendered model servers.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE
target="${1:-${MANIFESTO_SPEC:-}}"
if [[ "$target" == "--all" || -z "$target" ]]; then
  k delete deploy,lws,svc,cm,sa -l "app.kubernetes.io/name=manifesto,llm-d.ai/owner=${MANIFESTO_USER}" --ignore-not-found --wait=true
else
  render_model "$target" | k delete -f - --ignore-not-found --wait=true
fi
k wait --for=delete pod -l "llm-d.ai/inferenceServing=true,llm-d.ai/owner=${MANIFESTO_USER}" --timeout=10m 2>/dev/null || true
