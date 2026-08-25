#!/usr/bin/env bash
# Usage: vllm-args.sh [spec]   Print the effective vLLM server args per role as YAML (default: MANIFESTO_SPEC).
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env NAMESPACE
render_model "${1:-${MANIFESTO_SPEC:?MANIFESTO_SPEC must be set}}" | uv run --quiet --project "$MANIFESTO_ROOT" python scripts/vllm-args.py
