#!/usr/bin/env bash
# Usage: sweep.sh <sweep-name> [spec ...]
# For each spec: deploy -> benchmark -> record config metadata + logs -> teardown.
# BENCHMARK_WORKLOAD=agentx uses SWEEP_CONCURRENCIES; lmsys runs its pinned
# OpenHands multi-point sweep (parallel 1,2,4,8) in one client invocation.
# Layout (on PVC and after `just results`):  <RESULTS_PREFIX>/<sweep>/results_<config>/results_<config>_c<N>/
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE MODEL
sweep="${1:?usage: sweep.sh <sweep-name> [spec ...]}"; shift
specs=("$@"); [[ ${#specs[@]} -gt 0 ]] || read -r -a specs <<<"${SWEEP_SPECS:?set SWEEP_SPECS in .env or pass specs}"
benchmark_workload="${BENCHMARK_WORKLOAD:-agentx}"
case "$benchmark_workload" in
  agentx) read -r -a concurrencies <<<"${SWEEP_CONCURRENCIES:-16 32 64}" ;;
  lmsys) concurrencies=() ;;
  *) echo "ERROR: BENCHMARK_WORKLOAD must be agentx or lmsys" >&2; exit 2 ;;
esac
if [[ "$benchmark_workload" == agentx && "${AIPERF_RESULTS_MODE:-pvc}" == pvc ]]; then
  require_agentx_env RESULTS_PVC
fi
[[ "$sweep" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "ERROR: sweep name must be [A-Za-z0-9._-]" >&2; exit 2; }

failed=()
for spec in "${specs[@]}"; do
  config="$(basename "$spec" .yaml)"
  local_dir="results/.artifacts/sweeps/${sweep}/results_${config}"
  mkdir -p "$local_dir/logs"
  echo "################ $spec -> $local_dir"
  scripts/teardown-model.sh --all
  if ! scripts/deploy-model.sh "$spec"; then
    echo "FAILED: deploy $config" >&2; failed+=("${config}_deploy")
    for pod in $(k get pods -l 'llm-d.ai/inferenceServing=true' -o jsonpath='{.items[*].metadata.name}'); do
      k logs "$pod" --all-containers > "$local_dir/logs/${pod}.log" 2>&1 || true
    done
    scripts/teardown-model.sh --all
    continue
  fi
  selector="$(instance_selector "$spec")"
  render_model "$spec" > "$local_dir/manifest.yaml"
  scripts/vllm-args.sh "$spec" > "$local_dir/vllm-args.yaml"
  printf '%s\n' "$config" > "$local_dir/config_name.txt"
  k get pods -l "$selector" -o jsonpath='{.items[*].metadata.name}' | tr ' ' '|' > "$local_dir/pods.txt"
  manifesto config export models "$spec" -o "$local_dir/spec.yaml" --force >/dev/null
  grep -m1 '^  label:' "$local_dir/spec.yaml" | awk '{print $2}' > "$local_dir/model_label.txt" || true
  k get pods -l "$selector" -o jsonpath='{.items[0].spec.containers[?(@.name=="vllm")].image}' > "$local_dir/vllm_image.txt"
  scripts/vllm-build-info.sh "$selector" > "$local_dir/vllm_env.txt" || true
  scripts/kv-cache-info.sh "$selector" > "$local_dir/kv-cache.yaml" || true
  case "$benchmark_workload" in
    agentx)
      for c in "${concurrencies[@]}"; do
        echo "======== $config AgentX c=$c"
        if ! CONCURRENCY="$c" ARTIFACT_SUBDIR="${sweep}/results_${config}/results_${config}_c${c}" scripts/direct-run.sh; then
          echo "FAILED: $config c=$c" >&2; failed+=("${config}_c${c}")
        fi
      done
      ;;
    lmsys)
      echo "======== $config LMSYS OpenHands"
      if ! OUTPUT_DIR="$local_dir/lmsys" RUN_NAME="$config" scripts/lmsys-run.sh; then
        echo "FAILED: $config LMSYS OpenHands" >&2
        failed+=("${config}_lmsys")
      fi
      ;;
  esac
  if [[ "${ACCURACY_CHECK:-0}" == "1" ]]; then
    echo "======== $config accuracy"
    scripts/accuracy-check.sh "$local_dir/accuracy" || { echo "FAILED: $config accuracy" >&2; failed+=("${config}_accuracy"); }
  fi
  for pod in $(k get pods -l "$selector" -o jsonpath='{.items[*].metadata.name}'); do
    k logs "$pod" --all-containers > "$local_dir/logs/${pod}.log" 2>&1 || true
  done
  scripts/teardown-model.sh "$spec"
done
echo "Sweep '$sweep' ($benchmark_workload) done."
if [[ "$benchmark_workload" == agentx ]]; then
  echo "Results: results/.artifacts/sweeps/${sweep}"
else
  echo "LMSYS results are local under results/.artifacts/sweeps/${sweep}/results_*/lmsys/."
fi
[[ ${#failed[@]} -eq 0 ]] || { echo "Failed runs: ${failed[*]}" >&2; exit 1; }
