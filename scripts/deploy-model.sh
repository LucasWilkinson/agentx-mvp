#!/usr/bin/env bash
# Usage: deploy-model.sh [spec]   Render + apply a manifesto spec, wait for vLLM health via the router.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE MODEL
spec="${1:-${MANIFESTO_SPEC:?MANIFESTO_SPEC must be set}}"
selector="$(instance_selector "$spec")"
instance="${selector#app.kubernetes.io/instance=}"
instance="${instance%%,*}"

other="$(k get pods -l 'llm-d.ai/inferenceServing=true' -o jsonpath='{.items[*].metadata.labels.app\.kubernetes\.io/instance}' | tr ' ' '\n' | sort -u | grep -v "^${instance}$" || true)"
if [[ -n "$other" ]]; then
  echo "ERROR: other model servers are running (instance: $other). Run 'just teardown' first." >&2
  exit 1
fi

echo "Deploying $spec as $selector"
[[ -z "$VLLM_ENV" ]] || echo "vLLM env: $VLLM_ENV  image: ${VLLM_IMAGE:-<spec default>}"
echo "=== effective vLLM args ($spec)"; scripts/vllm-args.sh "$spec"
render_model "$spec" | k apply -f -
ready_deadline=$(( $(date +%s) + ${MODEL_READY_TIMEOUT_SECONDS:-1800} ))
while :; do
  pods_json="$(k get pods -l "$selector" -o json)"
  active_count="$(jq '[.items[] | select(.metadata.deletionTimestamp == null)] | length' <<<"$pods_json")"
  not_ready="$(jq '[.items[] | select(.metadata.deletionTimestamp == null) | select(any(.status.conditions[]?; .type == "Ready" and .status == "True") | not)] | length' <<<"$pods_json")"
  if (( active_count > 0 && not_ready == 0 )); then
    break
  fi
  (( $(date +%s) < ready_deadline )) || {
    echo "ERROR: model pods did not become ready in time" >&2
    k get pods -l "$selector" >&2
    exit 1
  }
  sleep 10
done

echo "Waiting for the router to serve $MODEL..."
port="${ROUTER_PROBE_PORT:-33080}"
k port-forward "service/${ROUTER_RELEASE}-epp" "${port}:80" >/dev/null 2>&1 &
pf=$!; trap 'kill $pf 2>/dev/null || true' EXIT
deadline=$(( $(date +%s) + ${MODEL_READY_TIMEOUT_SECONDS:-1800} ))
until curl -sf -m 5 "http://127.0.0.1:${port}/v1/models" 2>/dev/null | grep -q "\"$MODEL\""; do
  (( $(date +%s) < deadline )) || { echo "ERROR: router not serving $MODEL in time" >&2; k get pods -l "$selector" >&2; exit 1; }
  sleep 15
done
echo "Model is serving."
scripts/vllm-build-info.sh "$selector" | sed "s/^/vLLM build: /"
k get pods -l "$selector" -o wide
