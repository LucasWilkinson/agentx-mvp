#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   HF_TOKEN=... VLLM_SRC=/path/to/vllm CACHE_ROOT=/local/ssd/cache \
#     bash h200-serve-commands.sh fp8-wsfix
#
# Modes:
#   fp8-wsfix   GLM-5.2-FP8 + DeepEP-HT + DeepGEMM + PR 53914 (known IMA repro)
#   fp8         GLM-5.2-FP8 + DeepEP-HT + DeepGEMM, without PR 53914
#   fp8-debug   Same as fp8; use diagnostic commit cf83adbbe and log workspace shapes
#   mxfp4       RedHatAI MXFP4 experts + FP8 dense; H200 selects MarlinExperts
#   fp8-v2      GLM-5.2-FP8 + DeepEPv2 (known PCP ElasticBuffer assertion)

mode="${1:-fp8-wsfix}"
: "${HF_TOKEN:?export HF_TOKEN first}"
: "${VLLM_SRC:?set VLLM_SRC to the prepared vLLM checkout}"
CACHE_ROOT="${CACHE_ROOT:-/tmp/glm52-h200-cache}"
HF_HOME="${HF_HOME:-${CACHE_ROOT}/hf}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-16384}"

# Change these if the H200 host uses different interface names.
SOCKET_IFNAME="${SOCKET_IFNAME:-eth0}"
IB_HCA="${IB_HCA:-ibp}"

source "${VLLM_SRC}/.venv/bin/activate"

export HF_TOKEN HF_HOME
export HOME="${CACHE_ROOT}/home"
export XDG_CACHE_HOME="${CACHE_ROOT}/xdg"
export VLLM_CACHE_ROOT="${CACHE_ROOT}/vllm"
export FLASHINFER_CACHE_DIR="${CACHE_ROOT}/flashinfer"
export FLASHINFER_WORKSPACE_BASE="${CACHE_ROOT}/flashinfer-workspace"
export FLASH_ATTENTION_CUTE_DSL_CACHE_DIR="${CACHE_ROOT}/fa-cute-dsl"
export FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED=1
export TORCHINDUCTOR_CACHE_DIR="${CACHE_ROOT}/torchinductor"
export TILELANG_CACHE_DIR="${CACHE_ROOT}/tilelang"
export TRITON_CACHE_DIR="${CACHE_ROOT}/triton"
export TQDM_DISABLE=1
export VLLM_NO_USAGE_STATS=1
export VLLM_USE_V2_MODEL_RUNNER=1
export VLLM_SKIP_P2P_CHECK=1
export VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=1
export VLLM_PCP_DCP_DEBUG=0
export VLLM_MEM_TRACE=1
export VLLM_MEM_TRACE_SNAPSHOT=1

VLLM_DEBUG_WORKSPACE="${VLLM_DEBUG_WORKSPACE:-0}"
[[ "$mode" == "fp8-debug" ]] && VLLM_DEBUG_WORKSPACE=1
export VLLM_DEBUG_WORKSPACE

export GLOO_SOCKET_IFNAME="${SOCKET_IFNAME}"
export NCCL_SOCKET_IFNAME="${SOCKET_IFNAME}"
export NCCL_IB_HCA="${IB_HCA}"
export NVSHMEM_REMOTE_TRANSPORT=ibgda
export NVSHMEM_IB_ENABLE_IBGDA=true
export NVSHMEM_HCA_PREFIX="${IB_HCA}"
export NVSHMEM_BOOTSTRAP_UID_SOCK_IFNAME="${SOCKET_IFNAME}"
export NVIDIA_GDRCOPY=enabled

mkdir -p \
  "$HOME" "$HF_HOME" "$XDG_CACHE_HOME" "$VLLM_CACHE_ROOT" \
  "$FLASHINFER_CACHE_DIR" "$FLASHINFER_WORKSPACE_BASE" \
  "$FLASH_ATTENTION_CUTE_DSL_CACHE_DIR" "$TORCHINDUCTOR_CACHE_DIR" \
  "$TILELANG_CACHE_DIR" "$TRITON_CACHE_DIR"

case "$mode" in
  fp8-wsfix|fp8|fp8-debug)
    model=zai-org/GLM-5.2-FP8
    moe_backend=deep_gemm
    all2all_backend=deepep_high_throughput
    ;;
  mxfp4)
    model=RedHatAI/GLM-5.2-MXFP4xFP8_BLOCK
    moe_backend=auto
    all2all_backend=deepep_high_throughput
    ;;
  fp8-v2)
    model=zai-org/GLM-5.2-FP8
    moe_backend=deep_gemm
    all2all_backend=deepep_v2
    ;;
  *)
    echo "unknown mode: $mode" >&2
    exit 2
    ;;
esac

exec vllm serve "$model" \
  --device-ids 0,1,2,3,4,5,6,7 \
  --port 8000 \
  --tensor-parallel-size 1 \
  --enable-expert-parallel \
  --disable-access-log-for-endpoints /health,/v1/models,/metrics \
  --trust-remote-code \
  --max-model-len 163840 \
  --kv-cache-dtype fp8 \
  --moe-backend "$moe_backend" \
  --safetensors-load-strategy prefetch \
  --seed 0 \
  --disable-uvicorn-access-log \
  --max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS" \
  --gpu-memory-utilization 0.92 \
  --all2all-backend "$all2all_backend" \
  --prefill-context-parallel-size 8 \
  --decode-context-parallel-size 8 \
  --dcp-comm-backend ag_rs \
  -cc.cudagraph_mode=NONE
