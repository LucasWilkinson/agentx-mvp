#!/usr/bin/env bash
# Usage: vllm-build-info.sh <pod-selector>
# Per role (prefill, decode): env path, commit, branch, vLLM version and dirty files of the build running in
# that role's pod. Roles may run different builds (role env MANIFESTO_VLLM_ENV, see README).
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE
for role in prefill decode; do
  pod="$(k get pods -l "$1,llm-d.ai/role=$role" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [[ -n "$pod" ]] || continue
  echo "${role}:"
  k exec "$pod" -c vllm -- bash -c '
set -e
if [ -z "${MANIFESTO_VLLM_ENV:-}" ]; then echo "  env: <image>"; exit 0; fi
. "$MANIFESTO_VLLM_ENV/.venv/bin/activate"
src="$MANIFESTO_VLLM_ENV"
echo "  env: $src"
echo "  commit: $(git -C "$src" rev-parse HEAD 2>/dev/null || echo unknown)"
echo "  branch: $(git -C "$src" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
echo "  described: $(git -C "$src" describe --all --long 2>/dev/null || echo unknown)"
echo "  version: $(python -c "import vllm;print(vllm.__version__)" 2>/dev/null || echo unknown)"
git -C "$src" status --short 2>/dev/null | sed "s/^/  dirty: /"
'
done
