#!/usr/bin/env bash
# Install/upgrade the llm-d standalone router (Envoy + EPP) with deploy/router-values.yaml.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE
helm upgrade --install "$ROUTER_RELEASE" oci://ghcr.io/llm-d/charts/llm-d-router-standalone \
  --version "$ROUTER_CHART_VERSION" --kube-context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
  --values deploy/router-values.yaml \
  --set-string "router.modelServers.matchLabels.llm-d\.ai/owner=${MANIFESTO_USER}" \
  --wait --timeout 5m
echo "Router URL (in-cluster): http://${ROUTER_RELEASE}-epp:80"
