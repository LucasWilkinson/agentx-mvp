#!/usr/bin/env bash
# Run the pinned LMSYS OpenHands GLM agentic workload against a served model.
# If BASE_URL is unset, port-forward the configured llm-d router locally.
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/env.sh
load_agentx_env
require_agentx_env MODEL

SERVED_MODEL="${SERVED_MODEL:-$MODEL}"
TOKENIZER_MODEL="${TOKENIZER_MODEL:-${TOKENIZER:-zai-org/GLM-5.3}}"
OUTPUT_DIR="${OUTPUT_DIR:-$PWD/results/.artifacts/lmsys-glm-agentic}"
RUN_NAME="${RUN_NAME:-lmsys-openhands}"
LMSYS_LOCAL_PORT="${LMSYS_LOCAL_PORT:-18080}"
LMSYS_CACHE_DIR="${LMSYS_CACHE_DIR:-$PWD/results/.artifacts/cache/lmsys-glm-agentic}"
BLOG_COMMIT="${BLOG_COMMIT:-2bac7e166a7b5bf518b778817ec464cec0f75e3e}"
EVALSCOPE_COMMIT="${EVALSCOPE_COMMIT:-acd09b44384d53174768bb1063f675420f76fae9}"
BLOG_CHECKOUT="${BLOG_CHECKOUT:-$LMSYS_CACHE_DIR/sglang-$BLOG_COMMIT}"
CLIENT_VENV="${CLIENT_VENV:-$LMSYS_CACHE_DIR/evalscope-venv-$EVALSCOPE_COMMIT}"
CLIENT_PYTHON="${CLIENT_PYTHON:-python3.12}"
INSTALL_DEPS="${INSTALL_DEPS:-auto}"
LMSYS_NUMBERS="${LMSYS_NUMBERS:-4 8 8 16}"
LMSYS_PARALLELS="${LMSYS_PARALLELS:-1 2 4 8}"

case "$INSTALL_DEPS" in
  auto)
    if [[ -x "$CLIENT_VENV/bin/python" ]] &&
      "$CLIENT_VENV/bin/python" -c 'import evalscope.perf.plugin.datasets.swe_smith' >/dev/null 2>&1; then
      INSTALL_DEPS=0
    else
      INSTALL_DEPS=1
    fi
    ;;
  0|1) ;;
  *) echo "ERROR: INSTALL_DEPS must be auto, 0, or 1" >&2; exit 2 ;;
esac

port_forward_pid=""
cleanup() {
  if [[ -n "$port_forward_pid" ]]; then
    kill "$port_forward_pid" 2>/dev/null || true
    wait "$port_forward_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [[ -z "${BASE_URL:-}" ]]; then
  require_agentx_env KUBE_CONTEXT NAMESPACE ROUTER_RELEASE
  BASE_URL="http://127.0.0.1:$LMSYS_LOCAL_PORT"
  k port-forward "service/${ROUTER_RELEASE}-epp" "$LMSYS_LOCAL_PORT:80" >/dev/null 2>&1 &
  port_forward_pid=$!
  for _ in $(seq 1 60); do
    curl -sf -m 3 "$BASE_URL/health" >/dev/null 2>&1 && break
    kill -0 "$port_forward_pid" 2>/dev/null || {
      echo "ERROR: router port-forward exited" >&2
      exit 1
    }
    sleep 2
  done
fi

echo "LMSYS OpenHands: run=$RUN_NAME model=$SERVED_MODEL parallel=[$LMSYS_PARALLELS]"
BASE_URL="$BASE_URL" \
SERVED_MODEL="$SERVED_MODEL" \
TOKENIZER_MODEL="$TOKENIZER_MODEL" \
OUTPUT_DIR="$OUTPUT_DIR" \
RUN_NAME="$RUN_NAME" \
INSTALL_DEPS="$INSTALL_DEPS" \
BLOG_COMMIT="$BLOG_COMMIT" \
BLOG_CHECKOUT="$BLOG_CHECKOUT" \
EVALSCOPE_COMMIT="$EVALSCOPE_COMMIT" \
CLIENT_VENV="$CLIENT_VENV" \
CLIENT_PYTHON="$CLIENT_PYTHON" \
LMSYS_NUMBERS="$LMSYS_NUMBERS" \
LMSYS_PARALLELS="$LMSYS_PARALLELS" \
bash reproductions/glm53-benchmarks/lmsys-client.sh
