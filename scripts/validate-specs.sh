#!/usr/bin/env bash
# Validate every spec against its hardware-specific cluster profile.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env
H200_MANIFESTO_CLUSTER="${H200_MANIFESTO_CLUSTER:-coreweave-h200}"
GB200_MANIFESTO_CLUSTER="${GB200_MANIFESTO_CLUSTER:-oci-gb200}"
B200_MANIFESTO_CLUSTER="${B200_MANIFESTO_CLUSTER:-gke-b200}"
if rg -n --glob '*.yaml' 'cpu[_-]offload[_-]gb|--cpu-offload-gb' manifesto/models; then
  echo "Model-weight CPU offload is forbidden in model manifests" >&2
  exit 1
fi
while IFS= read -r f; do
  name="${f#manifesto/models/}"; name="${name%.yaml}"
  [[ "$(basename "$name")" == base* ]] && continue   # abstract layers (base, base-offload) have no release
  cluster="$H200_MANIFESTO_CLUSTER"
  [[ "$name" == */gb200/* ]] && cluster="$GB200_MANIFESTO_CLUSTER"
  [[ "$name" == */b200/* ]] && cluster="$B200_MANIFESTO_CLUSTER"
  manifesto config validate "$name" --cluster "$cluster" | tail -1 | sed "s|^|$name [$cluster]: |"
done < <(find manifesto/models -type f \( -name '*.yaml' -o -name '*.yml' \) | sort)
