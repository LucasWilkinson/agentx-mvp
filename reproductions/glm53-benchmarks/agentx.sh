#!/usr/bin/env bash
set -Eeuo pipefail

# Compact GLM AgentX runner. One invocation measures one cache mode so baseline
# and HiSparse can be scheduled independently on separate eight-GPU nodes.

ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"

COMMAND=${1:-sweep}
TARGET=${2:-baseline}

MODEL=${MODEL:-zai-org/GLM-5.3}
MODEL_REVISION=${MODEL_REVISION:-30333038ada1f1dacb294a93270305a890b50c14}
SERVED_MODEL_NAME=${SERVED_MODEL_NAME:-glm-agentx}
AGENTX_DATASET=${AGENTX_DATASET:-semianalysis_cc_traces_weka_062126}

# "limited" is the H200 profile. AIPerf admits an entire Weka trace when its
# peak context is <= 142,000 tokens; it never truncates an over-limit request.
# Use CONTEXT_PROFILE=full on B200 unless explicitly reproducing the limited
# population. Either limit remains independently overrideable.
CONTEXT_PROFILE=${CONTEXT_PROFILE:-limited}
case "$CONTEXT_PROFILE" in
  limited)
    MAX_MODEL_LEN=${MAX_MODEL_LEN:-142000}
    AIPERF_MAX_CONTEXT_LENGTH=${AIPERF_MAX_CONTEXT_LENGTH:-142000}
    ;;
  full)
    MAX_MODEL_LEN=${MAX_MODEL_LEN:-1048576}
    AIPERF_MAX_CONTEXT_LENGTH=${AIPERF_MAX_CONTEXT_LENGTH:-1048576}
    ;;
  *) echo "CONTEXT_PROFILE must be limited or full" >&2; exit 2 ;;
esac

PORT=${PORT:-8000}
MAX_NUM_SEQS=${MAX_NUM_SEQS:-256}
MAX_NUM_BATCHED_TOKENS=${MAX_NUM_BATCHED_TOKENS:-32768}
DEP_MAX_NUM_BATCHED_TOKENS=${DEP_MAX_NUM_BATCHED_TOKENS:-8192}
: "${GPU_MEMORY_UTILIZATION:?GPU_MEMORY_UTILIZATION must be explicitly set for this deployment}"
MTP_SPECULATIVE_TOKENS=${MTP_SPECULATIVE_TOKENS:-3}
ENFORCE_EAGER=${ENFORCE_EAGER:-0}
PARALLELISM=${PARALLELISM:-tep8}
HISPARSE_HOST_GIB=${HISPARSE_HOST_GIB:-800}
KV_OFFLOAD_MODE=${KV_OFFLOAD_MODE:-none}
KV_OFFLOAD_TOTAL_GIB=${KV_OFFLOAD_TOTAL_GIB:-800}
LOAD_FORMAT=${LOAD_FORMAT:-auto}
SKIP_CHAT_CORRECTNESS=${SKIP_CHAT_CORRECTNESS:-0}
SMOKE_ISL=${SMOKE_ISL:-128}
SMOKE_OSL=${SMOKE_OSL:-32}
SMOKE_REQUEST_COUNT=${SMOKE_REQUEST_COUNT:-8}
SMOKE_CONCURRENCY=${SMOKE_CONCURRENCY:-2}

AIPERF_DURATION=${AIPERF_DURATION:-1800}
if ((AIPERF_DURATION < 900)); then
  REQUIRE_SUBMISSION_VALID=0
  # AgentX locks publication runs to >=900s. This explicit override retains
  # workload semantics while correctly stamping shorter output invalid.
  AIPERF_SCENARIO_ARGS=(--unsafe-override)
else
  REQUIRE_SUBMISSION_VALID=1
  AIPERF_SCENARIO_ARGS=()
fi
MIN_RELATIVE_GAIN=${MIN_RELATIVE_GAIN:-0.01}
MAX_CONCURRENCY=${MAX_CONCURRENCY:-2048}
BASELINE_CONCURRENCIES=${BASELINE_CONCURRENCIES:-"1 8 32 64"}
HISPARSE_CONCURRENCIES=${HISPARSE_CONCURRENCIES:-"1 8 32 64 128 256"}
RANDOM_SEED=${RANDOM_SEED:-20260827}

RUN_ID=${RUN_ID:-glm53-agentx-$PARALLELISM-${AIPERF_DURATION}s}
RUN_TIMESTAMP=${RUN_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}
RESULTS_DIR=${RESULTS_DIR:-$ROOT/results/.artifacts/reproductions/glm53-agentx/$RUN_ID-$RUN_TIMESTAMP}
CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-0,1,2,3,4,5,6,7}
export CUDA_VISIBLE_DEVICES

SERVER_PID=
SERVER_LOG=

usage() {
  cat <<'EOF'
Usage: benchmarks/glm53_agentx.sh COMMAND [baseline|hisparse]

  smoke   Start one server, run the chat and eight-request AIPerf smoke, stop.
  sweep   Start one server, run the sparse probe schedule, continue doubling
          if still improving, add two log2 refinements, then report a table.
  report  Rebuild results.json/results.tsv and print the existing results.

Default concurrency probes:
  baseline: 1 8 32 64
  hisparse: 1 8 32 64 128 256

If every planned point improves throughput by MIN_RELATIVE_GAIN (default 1%),
probing continues by doubling. After the first non-improving point, two log2-
spaced points are measured between the best and non-improving concurrency.

Context populations:
  CONTEXT_PROFILE=limited  # default; inclusive AIPerf admission cap 142,000
  CONTEXT_PROFILE=full     # 1,048,576-token B200 population

AIPerf duration:
  AIPERF_DURATION=1800     # default full run
  AIPERF_DURATION=300      # quick exploratory run; submission-invalid

Parallelism:
  PARALLELISM=tep8         # default; TP8 with expert parallelism
  PARALLELISM=dep8         # TP1 x DP8 with expert parallelism
  PARALLELISM=tp8          # TP8 without expert parallelism
  DEP_MAX_NUM_BATCHED_TOKENS=8192  # DEP8-only default; other modes use 32768

GPU memory:
  GPU_MEMORY_UTILIZATION=1.0  # required; no deployment-independent default
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

cleanup_server() {
  if [[ -n ${SERVER_PID:-} ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill -TERM -- "-$SERVER_PID" 2>/dev/null || kill -TERM "$SERVER_PID" || true
    for _ in $(seq 1 60); do
      kill -0 "$SERVER_PID" 2>/dev/null || break
      sleep 1
    done
    kill -KILL -- "-$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  SERVER_PID=
}
trap cleanup_server EXIT INT TERM

wait_for_server() {
  local deadline=$((SECONDS + 3600))
  until curl --fail --silent "http://127.0.0.1:$PORT/health" >/dev/null; do
    kill -0 "$SERVER_PID" 2>/dev/null || {
      tail -n 200 "$SERVER_LOG" >&2 || true
      die "vLLM exited during startup; see $SERVER_LOG"
    }
    if grep -qE 'EngineCore failed to start|Worker failed with error' "$SERVER_LOG"; then
      tail -n 200 "$SERVER_LOG" >&2 || true
      die "vLLM engine failed during startup; see $SERVER_LOG"
    fi
    ((SECONDS < deadline)) || die "vLLM startup timed out; see $SERVER_LOG"
    sleep 5
  done
}

start_server() {
  local output_dir=$RESULTS_DIR/$TARGET
  local spec_config attention_config kv_transfer_config value offload_gib hisparse_gib
  local max_num_batched_tokens=$MAX_NUM_BATCHED_TOKENS
  local -a args cache_args eager_args load_args offload_args parallel_args speculative_args

  mkdir -p "$output_dir"
  SERVER_LOG=$output_dir/server.log
  printf 'GPU_MEMORY_UTILIZATION=%s\n' "$GPU_MEMORY_UTILIZATION" \
    | tee "$output_dir/run-config.env"
  if ((MTP_SPECULATIVE_TOKENS > 0)); then
    spec_config=$(jq -cn --argjson tokens "$MTP_SPECULATIVE_TOKENS" \
      '{method:"mtp",num_speculative_tokens:$tokens}')
    speculative_args=(--speculative-config "$spec_config")
  else
    speculative_args=()
  fi
  if ((ENFORCE_EAGER)); then
    eager_args=(--enforce-eager)
  else
    eager_args=()
  fi
  if [[ $LOAD_FORMAT == auto ]]; then
    load_args=()
  else
    load_args=(--load-format "$LOAD_FORMAT")
  fi

  case "$TARGET" in
    baseline) cache_args=() ;;
    hisparse)
      hisparse_gib=$HISPARSE_HOST_GIB
      [[ $PARALLELISM == dep8 ]] &&
        hisparse_gib=$(awk -v total="$HISPARSE_HOST_GIB" 'BEGIN { print total / 8 }')
      attention_config=$(jq -cn --argjson host "$hisparse_gib" \
        '{hisparse_config:{host_pool_gib:$host}}')
      cache_args=(--attention-config "$attention_config")
      ;;
    *) die "TARGET must be baseline or hisparse" ;;
  esac
  case "$PARALLELISM" in
    tep8) parallel_args=(-tp 8 -ep) ;;
    dep8)
      parallel_args=(-tp 1 -dp 8 -ep)
      max_num_batched_tokens=$DEP_MAX_NUM_BATCHED_TOKENS
      ;;
    tp8) parallel_args=(-tp 8) ;;
    *) die "PARALLELISM must be tep8, dep8, or tp8" ;;
  esac
  case "$KV_OFFLOAD_MODE" in
    none) offload_args=() ;;
    native)
      # vLLM's size is total across TP ranks but is instantiated once per DP
      # engine. Divide the requested node-wide budget across DP engines.
      offload_gib=$KV_OFFLOAD_TOTAL_GIB
      [[ $PARALLELISM == dep8 ]] &&
        offload_gib=$(awk -v total="$KV_OFFLOAD_TOTAL_GIB" 'BEGIN { print total / 8 }')
      kv_transfer_config=$(jq -cn --argjson gib "$offload_gib" '{
        kv_connector: "OffloadingConnector",
        kv_role: "kv_both",
        kv_connector_extra_config: {
          spec_name: "TieringOffloadingSpec",
          cpu_bytes_to_use: ($gib * 1073741824)
        }
      }')
      offload_args=(--kv-transfer-config "$kv_transfer_config")
      ;;
    *) die "KV_OFFLOAD_MODE must be none or native" ;;
  esac

  args=(
    vllm serve "$MODEL"
    --revision "$MODEL_REVISION"
    --served-model-name "$SERVED_MODEL_NAME"
    --trust-remote-code
    --host 127.0.0.1
    --port "$PORT"
    "${parallel_args[@]}"
    --kv-cache-dtype fp8
    --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
    --max-model-len "$MAX_MODEL_LEN"
    --max-num-seqs "$MAX_NUM_SEQS"
    --max-num-batched-tokens "$max_num_batched_tokens"
    --enable-prefix-caching
    "${load_args[@]}"
    "${offload_args[@]}"
    --enable-auto-tool-choice
    --tool-call-parser glm47
    --reasoning-parser glm45
    "${speculative_args[@]}"
    "${eager_args[@]}"
    "${cache_args[@]}"
  )
  {
    printf 'env'
    for value in VLLM_SERVER_DEV_MODE=1 "${args[@]}"; do printf ' %q' "$value"; done
    printf '\n'
  } >"$output_dir/server-command.txt"

  date -u +%Y-%m-%dT%H:%M:%SZ >"$output_dir/server-started-at-utc.txt"
  setsid env VLLM_SERVER_DEV_MODE=1 "${args[@]}" >"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  wait_for_server
  date -u +%Y-%m-%dT%H:%M:%SZ >"$output_dir/server-ready-at-utc.txt"
}

smoke() {
  local output_dir=$RESULTS_DIR/$TARGET/validate
  local artifacts=$output_dir/aiperf-smoke
  mkdir -p "$output_dir"

  if ((SKIP_CHAT_CORRECTNESS == 0)); then
    curl --fail --silent --show-error \
      "http://127.0.0.1:$PORT/v1/chat/completions" \
      -H 'Content-Type: application/json' \
      -d "$(jq -cn --arg model "$SERVED_MODEL_NAME" \
        '{model:$model,messages:[{role:"user",content:"What is 19 + 23? Reply with only the number."}],chat_template_kwargs:{enable_thinking:false},temperature:0,max_tokens:16}')" \
      | tee "$output_dir/response.json" \
      | jq -e '(.choices[0].message.content // "") | gsub("\\s"; "") == "42"' \
        >/dev/null || die "Chat smoke failed"
  fi

  aiperf profile \
    --url "http://127.0.0.1:$PORT" \
    --model "$SERVED_MODEL_NAME" \
    --tokenizer "$MODEL" \
    --tokenizer-revision "$MODEL_REVISION" \
    --endpoint-type chat \
    --streaming \
    --isl "$SMOKE_ISL" \
    --osl "$SMOKE_OSL" \
    --request-count "$SMOKE_REQUEST_COUNT" \
    --concurrency "$SMOKE_CONCURRENCY" \
    --random-seed "$RANDOM_SEED" \
    --use-server-token-count \
    --artifact-dir "$artifacts" \
    --ui none
  jq -e --argjson count "$SMOKE_REQUEST_COUNT" \
    '(.request_count.avg == $count) and ((.error_request_count.avg // 0) == 0)' \
    "$artifacts/profile_export_aiperf.json" >/dev/null || die "AIPerf smoke failed"
}

reset_caches() {
  local response
  for _ in $(seq 1 60); do
    response=$(curl --fail --silent --show-error -X POST \
      "http://127.0.0.1:$PORT/reset_prefix_cache?reset_external=true")
    [[ $(jq -r '.success' <<<"$response") == true ]] && return
    sleep 2
  done
  die "Could not reset local and connector prefix caches"
}

run_point() {
  local concurrency=$1
  local point_dir=$RESULTS_DIR/$TARGET/concurrency-$concurrency
  local artifacts=$point_dir/artifacts
  local summary=$artifacts/profile_export_aiperf.json
  mkdir -p "$point_dir"

  local validity_filter='(.output_token_throughput.avg > 0) and
    (.output_token_throughput_per_user.avg > 0)'
  if [[ $REQUIRE_SUBMISSION_VALID == 1 ]]; then
    validity_filter=".metadata.submission_valid == true and ($validity_filter)"
  fi

  if [[ -s $summary ]] && jq -e "$validity_filter" "$summary" >/dev/null; then
    echo "Reusing valid concurrency=$concurrency"
    return
  fi

  echo "Running $TARGET concurrency=$concurrency for ${AIPERF_DURATION}s"
  local -a client_args=(
    aiperf profile
    --scenario inferencex-agentx-mvp
    "${AIPERF_SCENARIO_ARGS[@]}"
    --url "http://127.0.0.1:$PORT"
    --model "$SERVED_MODEL_NAME"
    --tokenizer "$MODEL"
    --tokenizer-revision "$MODEL_REVISION"
    --endpoint-type chat
    --public-dataset "$AGENTX_DATASET"
    --max-context-length "$AIPERF_MAX_CONTEXT_LENGTH"
    --concurrency "$concurrency"
    --benchmark-duration "$AIPERF_DURATION"
    --random-seed "$RANDOM_SEED"
    --use-server-token-count
    --artifact-dir "$artifacts"
    --ui none
  )
  date -u +%Y-%m-%dT%H:%M:%SZ >"$point_dir/started-at-utc.txt"
  env | sort >"$point_dir/client-environment.txt"
  cp "$RESULTS_DIR/$TARGET/server-command.txt" "$point_dir/server-command.txt"
  cp "$RESULTS_DIR/$TARGET/run-config.env" "$point_dir/server-run-config.env"
  printf '%q ' "${client_args[@]}" >"$point_dir/client-command.txt"
  printf '\n' >>"$point_dir/client-command.txt"
  "${client_args[@]}" \
    2>&1 | tee "$point_dir/aiperf.log"
  date -u +%Y-%m-%dT%H:%M:%SZ >"$point_dir/finished-at-utc.txt"

  jq -e "$validity_filter" "$summary" >/dev/null ||
    die "Invalid AgentX point: $summary"
  curl --fail --silent "http://127.0.0.1:$PORT/metrics" \
    >"$point_dir/vllm-metrics.txt"
  if [[ -x /input/plot-live-points.py ]]; then
    RESULTS_DIR="$RESULTS_DIR" /input/plot-live-points.py || true
  fi
}

throughput() {
  jq -er '.output_token_throughput.avg' \
    "$RESULTS_DIR/$TARGET/concurrency-$1/artifacts/profile_export_aiperf.json"
}

is_improvement() {
  awk -v current="$1" -v best="$2" -v gain="$MIN_RELATIVE_GAIN" \
    'BEGIN { exit !(current > best * (1 + gain)) }'
}

measure_probe() {
  local concurrency=$1
  reset_caches
  run_point "$concurrency"
}

sweep_points() {
  ((AIPERF_DURATION >= 60)) || die "AIPERF_DURATION must be >= 60"

  local schedule_text
  case "$TARGET" in
    baseline) schedule_text=$BASELINE_CONCURRENCIES ;;
    hisparse) schedule_text=$HISPARSE_CONCURRENCIES ;;
    *) die "TARGET must be baseline or hisparse" ;;
  esac

  local -a schedule refinements
  read -r -a schedule <<<"$schedule_text"
  ((${#schedule[@]} > 0)) || die "Concurrency schedule is empty"

  local previous=0 concurrency current
  for concurrency in "${schedule[@]}"; do
    [[ $concurrency =~ ^[1-9][0-9]*$ ]] || die "Invalid concurrency: $concurrency"
    ((concurrency > previous)) || die "Concurrency schedule must increase"
    ((concurrency <= MAX_CONCURRENCY)) || die "$concurrency exceeds MAX_CONCURRENCY"
    previous=$concurrency
  done

  local best_concurrency=0 best_throughput=0 bad_concurrency=0
  local last_concurrency=0
  for concurrency in "${schedule[@]}"; do
    measure_probe "$concurrency"
    current=$(throughput "$concurrency")
    last_concurrency=$concurrency
    if is_improvement "$current" "$best_throughput"; then
      best_concurrency=$concurrency
      best_throughput=$current
    else
      bad_concurrency=$concurrency
      break
    fi
  done

  # The sparse schedule saves intermediate points, but never declares a peak
  # merely because the planned list ended while throughput was still rising.
  concurrency=$((last_concurrency * 2))
  while ((bad_concurrency == 0 && concurrency <= MAX_CONCURRENCY)); do
    measure_probe "$concurrency"
    current=$(throughput "$concurrency")
    if is_improvement "$current" "$best_throughput"; then
      best_concurrency=$concurrency
      best_throughput=$current
      concurrency=$((concurrency * 2))
    else
      bad_concurrency=$concurrency
    fi
  done

  if ((bad_concurrency > best_concurrency)); then
    mapfile -t refinements < <(python - "$best_concurrency" "$bad_concurrency" <<'PY'
import math
import sys

lo, hi = map(int, sys.argv[1:])
for i in (1, 2):
    print(round(2 ** (math.log2(lo) + i * (math.log2(hi) - math.log2(lo)) / 3)))
PY
    )
    for concurrency in "${refinements[@]}"; do
      ((concurrency > best_concurrency && concurrency < bad_concurrency)) || continue
      reset_caches
      run_point "$concurrency"
    done
  else
    echo "WARNING: no plateau by MAX_CONCURRENCY=$MAX_CONCURRENCY" >&2
  fi
}

report_results() {
  mkdir -p "$RESULTS_DIR"
  shopt -s nullglob
  local -a summaries=(
    "$RESULTS_DIR"/baseline/concurrency-*/artifacts/profile_export_aiperf.json
    "$RESULTS_DIR"/hisparse/concurrency-*/artifacts/profile_export_aiperf.json
  )
  shopt -u nullglob
  ((${#summaries[@]} > 0)) || die "No AIPerf result JSON found in $RESULTS_DIR"

  local summary point_dir mode concurrency relative_path
  {
    for summary in "${summaries[@]}"; do
      point_dir=$(dirname "$(dirname "$summary")")
      mode=$(basename "$(dirname "$point_dir")")
      concurrency=${point_dir##*-}
      relative_path=${summary#"$RESULTS_DIR"/}
      jq \
        --arg mode "$mode" \
        --arg parallelism "$PARALLELISM" \
        --argjson duration "$AIPERF_DURATION" \
        --argjson concurrency "$concurrency" \
        --arg result_json "$relative_path" \
        '{
          mode: $mode,
          parallelism: $parallelism,
          duration_seconds: $duration,
          concurrency: $concurrency,
          output_token_throughput: .output_token_throughput.avg,
          output_token_throughput_per_user: .output_token_throughput_per_user.avg,
          request_count: .request_count.avg,
          submission_valid: (.metadata.submission_valid // false),
          result_json: $result_json
        }' "$summary"
    done
  } | jq -s 'sort_by(.mode, .concurrency)' >"$RESULTS_DIR/results.json"

  jq -r '
    ["mode", "parallelism", "duration_s", "concurrency", "output_tok/s", "output_tok/s/user", "requests", "valid"],
    (.[] | [
      .mode,
      .parallelism,
      .duration_seconds,
      .concurrency,
      ((.output_token_throughput * 100 | round) / 100),
      ((.output_token_throughput_per_user * 100 | round) / 100),
      .request_count,
      .submission_valid
    ]) | @tsv
  ' "$RESULTS_DIR/results.json" >"$RESULTS_DIR/results.tsv"

  echo "Results: $RESULTS_DIR/results.json"
  if command -v column >/dev/null 2>&1; then
    column -t -s $'\t' "$RESULTS_DIR/results.tsv"
  else
    cat "$RESULTS_DIR/results.tsv"
  fi
}

sweep_command() {
  start_server
  smoke
  sweep_points
  report_results
  cleanup_server
}

smoke_command() {
  start_server
  smoke
  cleanup_server
}

case "$COMMAND" in
  smoke) smoke_command ;;
  sweep) sweep_command ;;
  report) report_results ;;
  -h|--help|help) usage ;;
  *) usage; exit 2 ;;
esac
