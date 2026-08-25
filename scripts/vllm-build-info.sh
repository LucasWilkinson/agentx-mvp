#!/usr/bin/env bash
# Usage: vllm-build-info.sh <pod-selector>
# Print env path, commit, branch, vLLM version and dirty files of the build running in the prefill pod.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE
pod="$(k get pods -l "$1,llm-d.ai/role=prefill" -o jsonpath='{.items[0].metadata.name}')"
k exec "$pod" -c vllm -- bash -c '
set -e
if [ -z "${MANIFESTO_VLLM_DEV_VENV:-}" ]; then echo "env=<image>"; exit 0; fi
. "$MANIFESTO_VLLM_DEV_VENV/bin/activate"
src="$(dirname "$MANIFESTO_VLLM_DEV_VENV")"
echo "env=$src"
echo "commit=$(git -C "$src" rev-parse HEAD 2>/dev/null || echo unknown)"
echo "branch=$(git -C "$src" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
echo "version=$(python -c "import vllm;print(vllm.__version__)" 2>/dev/null || echo unknown)"
git -C "$src" status --short 2>/dev/null | sed "s/^/dirty: /"
'
