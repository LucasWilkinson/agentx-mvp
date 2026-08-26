#!/usr/bin/env bash
# Deploy each memtrace spec, save the prefill pod's MEMTRACE + memory-profiler lines, tear down.
# Usage: memtrace-run.sh <out-dir> spec...
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE
out="${1:?out-dir}"; shift; mkdir -p "$out"
for spec in "$@"; do
  name="$(basename "$spec" .yaml)"
  scripts/teardown-model.sh --all
  scripts/deploy-model.sh "$spec" || echo "deploy failed: $name (collecting logs anyway)"
  for pod in $(k get pods -l 'llm-d.ai/inferenceServing=true' -o jsonpath='{.items[*].metadata.name}'); do
    k logs "$pod" -c vllm > "$out/${name}_${pod}.log" 2>&1 || true
    grep -E "MEMTRACE|Actual usage is|Available KV cache memory|Model loading took" "$out/${name}_${pod}.log" | grep -E "PCP0|EngineCore" | sed -E 's/.*\] //' > "$out/${name}_summary.txt" || true
  done
  scripts/teardown-model.sh --all
done
echo "memtrace done: $out"
