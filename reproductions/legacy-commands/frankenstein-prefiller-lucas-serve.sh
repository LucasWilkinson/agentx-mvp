#!/usr/bin/env bash
set -euo pipefail

: "${VLLM_SRC:?set VLLM_SRC to the frankenstein-prefiller-lucas checkout}"

source "${VLLM_SRC}/.venv/bin/activate"

export VLLM_USE_PCP_DIRECT_KV=1
export VLLM_WORKER_MULTIPROC_METHOD=spawn

exec vllm serve nvidia/GLM-5.2-NVFP4 \
  --trust-remote-code \
  --tensor-parallel-size 1 \
  --prefill-context-parallel-size 4 \
  --enable-expert-parallel \
  --kv-cache-dtype fp8 \
  --max-num-batched-tokens 32768 \
  --max-num-seqs 256 \
  --speculative-config.method dspark \
  --speculative-config.model RedHatAI/GLM-5.2-speculator.dspark \
  --speculative-config.num-speculative-tokens 7 \
  --speculative-config.draft-sample-method probabilistic \
  --port 8002 \
  --no-enable-log-requests
