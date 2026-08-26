#!/usr/bin/env bash
# Usage: profile-prefill.sh <spec> [isl_words] [port]
# Deploy a prefill-only spec (no router), warm it with random long prompts, capture one torch-profiler
# trace of a single prefill, then summarise the traces on the devbox. Traces stay on the workspace PVC.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE MODEL
spec="$1"; words="${2:-24000}"; port="${3:-8000}"
name="$(basename "$spec" .yaml)"
selector="$(instance_selector "$spec")"
render_model "$spec" | k apply -f -
k wait --for=condition=Ready pod -l "$selector" --timeout="${MODEL_READY_TIMEOUT:-30m}"
pod="$(k get pods -l "$selector" -o jsonpath='{.items[0].metadata.name}')"
ip="$(k get pod "$pod" -o jsonpath='{.status.podIP}')"
echo "pod=$pod ip=$ip"
until k exec devbox -- curl -sf -m 5 "http://$ip:$port/v1/models" >/dev/null; do sleep 10; done
k exec -i devbox -- env IP="$ip" PORT="$port" WORDS="$words" MODEL="$MODEL" NAME="$name" python3 - < scripts/profile-requests.py
echo "traces:"; k exec devbox -- sh -c "sleep 20; ls -la /workspace/profiles/$name/ | tail -12"
