#!/usr/bin/env bash
# Usage: render-model.sh [spec]   (default: MANIFESTO_SPEC from .env)
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env NAMESPACE
render_model "${1:-${MANIFESTO_SPEC:?MANIFESTO_SPEC must be set}}"
