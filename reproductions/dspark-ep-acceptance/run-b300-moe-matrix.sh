#!/usr/bin/env bash
set -euo pipefail

REPO=${REPO:-/home/LucasWilkinson/local/vllm-lopez-frankenstein-ab6}
HARNESS_DIR=${HARNESS_DIR:-$PWD}
OUT_DIR=${OUT_DIR:-/tmp/dspark-ep-acceptance-ab6}
HF_HOME=${HF_HOME:-/data/LucasWilkinson/hub_cache}
MODEL=${MODEL:-nvidia/GLM-5.2-NVFP4}
SPEC_MODEL=${SPEC_MODEL:-RedHatAI/GLM-5.2-speculator.dspark}
PORT=${PORT:-8102}

mkdir -p "$OUT_DIR"
printf '%s\n' "$(git -C "$REPO" rev-parse HEAD)" >"$OUT_DIR/commit.txt"

server_pid=
cleanup() {
  if [[ -n "$server_pid" ]]; then
    kill -TERM "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

run_cell() {
  local name=$1 backend=$2 ep=$3
  local cell="$OUT_DIR/$name"
  mkdir -p "$cell"
  local ep_args=()
  [[ "$ep" == 1 ]] && ep_args+=(--enable-expert-parallel)
  local spec
  # The target already uses FP8 KV. Let the draft share that CacheConfig so the
  # engine's resolved KV layout is propagated before DSpark graph profiling.
  spec=$(printf '{"method":"dspark","model":"%s","num_speculative_tokens":7,"draft_sample_method":"greedy"}' "$SPEC_MODEL")

  HF_HOME="$HF_HOME" PYTHONPATH="$REPO" VLLM_WORKER_MULTIPROC_METHOD=spawn \
    VLLM_ALLREDUCE_USE_FLASHINFER=0 \
    "$REPO/.venv/bin/vllm" serve "$MODEL" \
      --served-model-name dspark-acceptance \
      --host 127.0.0.1 --port "$PORT" \
      --trust-remote-code \
      --tensor-parallel-size 4 \
      "${ep_args[@]}" \
      --moe-backend "$backend" \
      --kv-cache-dtype fp8 \
      --max-model-len 4096 \
      --max-num-batched-tokens 4096 \
      --max-num-seqs 8 \
      --gpu-memory-utilization 0.90 \
      --compilation-config '{"pass_config":{"fuse_allreduce_rms":false}}' \
      --speculative-config "$spec" \
      >"$cell/server.log" 2>&1 &
  server_pid=$!

  for _ in $(seq 1 240); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break
    kill -0 "$server_pid" 2>/dev/null || {
      tail -100 "$cell/server.log"
      return 1
    }
    sleep 5
  done
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null
  HF_HOME="$HF_HOME" PYTHONPATH="$REPO" "$REPO/.venv/bin/python" \
    "$HARNESS_DIR/probe.py" \
    --url "http://127.0.0.1:$PORT" \
    --model dspark-acceptance \
    --tokenizer "$MODEL" \
    --output "$cell/result.json" | tee "$cell/summary.json"

  kill -TERM "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  server_pid=
  sleep 5
}

run_cell trtllm-tp4 flashinfer_trtllm 0
run_cell trtllm-tp4-ep flashinfer_trtllm 1
run_cell cutlass-tp4 flashinfer_cutlass 0
run_cell cutlass-tp4-ep flashinfer_cutlass 1
