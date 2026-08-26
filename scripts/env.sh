#!/usr/bin/env bash
# Shared helpers. Scripts: cd to repo root, source this, call load_agentx_env + require_agentx_env.

load_agentx_env() {
  local env_file="${ENV_FILE:-.env}"
  if [[ -f "$env_file" ]]; then
    # Variables already set in the shell win over .env, so `VLLM_ENV=... just deploy` works.
    local line name
    set -a
    while IFS= read -r line; do
      [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)= ]] || continue
      name="${BASH_REMATCH[1]}"
      [[ -n "${!name+x}" ]] || eval "$line"
    done < "$env_file"
    set +a
  fi
  MANIFESTO_ROOT="${MANIFESTO_ROOT:-../llm-manifesto}"
  MANIFESTO_USER="${MANIFESTO_USER:-$USER}"
  MANIFESTO_CLUSTER="${MANIFESTO_CLUSTER:-coreweave-h200}"
  ROUTER_RELEASE="${ROUTER_RELEASE:-glm}"
  ROUTER_CHART_VERSION="${ROUTER_CHART_VERSION:-v0.9.0}"
  RESULTS_PREFIX="${RESULTS_PREFIX:-agentx}"
  # vLLM build under test: a vllm-envs (`ve`) env or clone with .venv on the workspace PVC,
  # plus the toolchain image it runs in. Both are recorded with every result.
  VLLM_ENV="${VLLM_ENV:-}"
  VLLM_IMAGE="${VLLM_IMAGE:-}"
  export MANIFESTO_ROOT MANIFESTO_USER MANIFESTO_CLUSTER ROUTER_RELEASE ROUTER_CHART_VERSION RESULTS_PREFIX VLLM_ENV VLLM_IMAGE
    : "${KUEUE_QUEUE:=}"; export KUEUE_QUEUE
  export MANIFESTO_CONFIG_HOME="$PWD/manifesto"
}

require_agentx_env() {
  local name
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      echo "ERROR: $name must be set in ${ENV_FILE:-.env}" >&2
      exit 2
    fi
  done
}

k() { kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" "$@"; }

manifesto() {
  [[ -d "$MANIFESTO_ROOT" ]] || { echo "ERROR: MANIFESTO_ROOT=$MANIFESTO_ROOT not found (git clone https://github.com/neuralmagic/llm-manifesto)" >&2; exit 2; }
  uv run --quiet --project "$MANIFESTO_ROOT" manifesto "$@"
}

# Kubernetes label selector for the model-server pods of a spec.
instance_selector() {
  local spec="$1"
  local instance
  instance="$(render_model "$spec" | awk '/app.kubernetes.io\/instance:/ && !seen {seen=1; print $2}')"
  [[ -n "$instance" ]] || { echo "ERROR: could not determine manifesto instance for $spec" >&2; exit 1; }
  printf 'app.kubernetes.io/instance=%s,llm-d.ai/inferenceServing=true' "$instance"
}

# Render only the model-server objects for a spec; the llm-d router is managed separately (scripts/router.sh).
render_model() {
  local spec="$1"
  local dev_args=()
  if [[ -n "$VLLM_ENV" ]]; then
    # neuralmagic/llm-manifesto takes the vllm-envs worktree as --vllm-env; older forks used --dev-venv/--dev-source.
    if manifesto render manifest --help 2>/dev/null | grep -q -- '--vllm-env'; then
      dev_args=(--vllm-env "$VLLM_ENV")
    else
      dev_args=(--dev-venv "$VLLM_ENV/.venv" --dev-source "$VLLM_ENV")
    fi
  fi
  manifesto render manifest "$spec" --cluster "$MANIFESTO_CLUSTER" --namespace "$NAMESPACE" --user "$MANIFESTO_USER" "${dev_args[@]}" \
    | VLLM_IMAGE="$VLLM_IMAGE" uv run --quiet --project "$MANIFESTO_ROOT" python scripts/filter-render.py
}
