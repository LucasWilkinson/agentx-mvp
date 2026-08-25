#!/usr/bin/env bash
# Usage: sweep.sh <sweep-name> [spec ...]
# For each spec: deploy -> benchmark each SWEEP_CONCURRENCIES -> record config metadata + logs -> teardown.
# Layout (on PVC and after `just results`):  <RESULTS_PREFIX>/<sweep>/results_<config>/results_<config>_c<N>/
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE MODEL RESULTS_PVC
sweep="${1:?usage: sweep.sh <sweep-name> [spec ...]}"; shift
specs=("$@"); [[ ${#specs[@]} -gt 0 ]] || read -r -a specs <<<"${SWEEP_SPECS:?set SWEEP_SPECS in .env or pass specs}"
read -r -a concurrencies <<<"${SWEEP_CONCURRENCIES:-16 32 64}"
[[ "$sweep" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "ERROR: sweep name must be [A-Za-z0-9._-]" >&2; exit 2; }

failed=()
for spec in "${specs[@]}"; do
  config="$(basename "$spec" .yaml)"
  local_dir="results/${sweep}/results_${config}"
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
  for c in "${concurrencies[@]}"; do
    echo "======== $config c=$c"
    if ! CONCURRENCY="$c" ARTIFACT_SUBDIR="${sweep}/results_${config}/results_${config}_c${c}" scripts/direct-run.sh; then
      echo "FAILED: $config c=$c" >&2; failed+=("${config}_c${c}")
    fi
  done
  for pod in $(k get pods -l "$selector" -o jsonpath='{.items[*].metadata.name}'); do
    k logs "$pod" --all-containers > "$local_dir/logs/${pod}.log" 2>&1 || true
  done
  scripts/teardown-model.sh "$spec"
done
echo "Sweep '$sweep' done. Download with: just results && python3 gen_interactivity_chart.py results/${sweep}"
[[ ${#failed[@]} -eq 0 ]] || { echo "Failed runs: ${failed[*]}" >&2; exit 1; }
