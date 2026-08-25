#!/usr/bin/env bash
# One-time namespace prerequisites for manifesto-rendered pods: HF token secret and image pull secret on the default SA.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE
token="${HF_TOKEN:-$(cat ~/.cache/huggingface/token 2>/dev/null || true)}"
[[ -n "$token" ]] || { echo "ERROR: set HF_TOKEN or log in with 'hf auth login'" >&2; exit 2; }
k create secret generic hf-secret --from-literal=HF_TOKEN="$token" --dry-run=client -o yaml | k apply -f -
pull_secret="${IMAGE_PULL_SECRET:-quay-push}"
k get secret "$pull_secret" >/dev/null
k patch serviceaccount default -p "{\"imagePullSecrets\":[{\"name\":\"${pull_secret}\"}]}"
echo "Bootstrap complete."
