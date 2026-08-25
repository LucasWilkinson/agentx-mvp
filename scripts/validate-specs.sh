#!/usr/bin/env bash
# Validate every spec in manifesto/models against the configured cluster profile.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env
for f in manifesto/models/*/*.yaml; do
  name="${f#manifesto/models/}"; name="${name%.yaml}"
  [[ "$(basename "$name")" == base* ]] && continue   # abstract layers (base, base-offload) have no release
  manifesto config validate "$name" --cluster "$MANIFESTO_CLUSTER" | tail -1 | sed "s|^|$name: |"
done
