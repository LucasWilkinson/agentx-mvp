#!/usr/bin/env bash
# Usage: kv-cache-info.sh <pod-selector>
# Print per-role vLLM memory/KV-cache facts (first engine rank of each pod) as YAML, scraped from pod logs:
# weights loaded, KV cache memory, KV cache tokens, max concurrency at max_model_len, auto-fit max_model_len.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE
selector="${1:?usage: kv-cache-info.sh <pod-selector>}"
for pod in $(k get pods -l "$selector" -o jsonpath='{.items[*].metadata.name}'); do
  role="$(k get pod "$pod" -o jsonpath='{.metadata.labels.llm-d\.ai/role}')"
  echo "${role:-$pod}:"
  echo "  pod: $pod"
  k logs "$pod" -c vllm 2>/dev/null \
    | grep -E "Model loading took|Available KV cache memory|GPU KV cache size|Auto-fit max_model_len|Graph capturing finished" \
    | grep -vE "_(TP|DP)[1-9][0-9]*(_|\b)" \
    | sed -E 's/^.*\] //' \
    | awk '!seen[$1$2$3]++' \
    | python3 scripts/kv-cache-info.py
done
