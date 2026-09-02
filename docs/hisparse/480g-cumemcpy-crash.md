# HiSparse 480 GiB batched-DMA crash reproducer

This reproduces the TP8 GLM-5.3 failure observed on eight H200 GPUs with
HiSparse commit `cd4b523d84f5fac2cdeebb45f2b575b26d402b53`.

## Requirements

- One host with 8 H200 GPUs and at least 640 GiB of `/dev/shm`
- At least 512 GiB available host memory
- CUDA 13 environment capable of building/running the target vLLM commit
- Python 3.12 for the benchmark client

The cluster run used image `quay.io/rh-ee-lwilkins/vllm-envs-cuda:pr8-cv2`.
Build and install the CUDA extensions from the checked-out commit; do not use
an extension binary from a different commit.

```bash
git clone https://github.com/neuralmagic/vllm.git vllm-hisparse
cd vllm-hisparse
git checkout cd4b523d84f5fac2cdeebb45f2b575b26d402b53
# Install/build this checkout using the normal vLLM development procedure for
# the machine, then activate that environment.
```

## Server

Remove stale mmap files before every attempt:

```bash
find /dev/shm -maxdepth 1 -type f -name 'vllm_offload_*.mmap' -delete

VLLM_ENGINE_READY_TIMEOUT_S=1800 \
vllm serve zai-org/GLM-5.3 \
  --revision 30333038ada1f1dacb294a93270305a890b50c14 \
  --served-model-name glm-agentx \
  --trust-remote-code \
  --host 127.0.0.1 \
  --port 8000 \
  -tp 8 \
  --kv-cache-dtype fp8 \
  --gpu-memory-utilization 0.92 \
  --max-model-len 142000 \
  --max-num-seqs 256 \
  --max-num-batched-tokens 32768 \
  --enable-prefix-caching \
  --kv-transfer-config '{"kv_connector":"OffloadingConnector","kv_role":"kv_both","kv_connector_extra_config":{"spec_name":"TieringOffloadingSpec","cpu_bytes_to_use":34359738368}}' \
  --attention-config '{"hisparse_config":{"host_pool_gib":480}}' \
  --speculative-config '{"method":"mtp","num_speculative_tokens":3}' \
  --enable-auto-tool-choice \
  --tool-call-parser glm47 \
  --reasoning-parser glm45 \
  2>&1 | tee server.log
```

Initialization of the 480 GiB shared mmap can take several minutes. Wait for:

```bash
curl --fail http://127.0.0.1:8000/health
```

## Client

Prepare the pinned client once. Keep these generated files outside the vLLM
checkout:

```bash
mkdir -p results/.artifacts/reproductions/glm-5.3-30333038/hisparse-480g-crash
cd results/.artifacts/reproductions/glm-5.3-30333038/hisparse-480g-crash

git clone https://github.com/Jiminator/sglang.git lmsys-repro
git -C lmsys-repro checkout 2bac7e166a7b5bf518b778817ec464cec0f75e3e

python3.12 -m venv client-venv
source client-venv/bin/activate
pip install 'modelscope[datasets]==1.34.0' 'lxml==6.0.2'
pip install 'evalscope[perf] @ git+https://github.com/modelscope/evalscope.git@acd09b44384d53174768bb1063f675420f76fae9'

python lmsys-repro/benchmark/glm_nvfp4_blog/build_openhands_padded_dataset.py \
  --model zai-org/GLM-5.3 \
  --pad-source openscience \
  --first-turn-length 74160 \
  --subsequent-turn-length 753 \
  --num-turns 13 \
  --number 128 \
  --output-path openhand-zai-org-GLM-5.3.json
```

Then run the c32 workload directly:

```bash
source client-venv/bin/activate

evalscope perf \
  --model glm-agentx \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --api openai \
  --dataset swe_smith \
  --dataset-path "$PWD/openhand-zai-org-GLM-5.3.json" \
  --dataset-offset 52 \
  --max-tokens 220 \
  --multi-turn \
  --number 64 \
  --parallel 32 \
  --extra-args '{"ignore_eos":true}' \
  --name tp8-hisparse480-native32-c32 \
  --outputs-dir "$PWD/results" \
  --no-timestamp
```

## Expected failure

The observed run failed during the first cold wave, with 18 requests running,
14 waiting, and GPU KV usage near saturation. The relevant stack is:

```text
model_runner.execute_model
  kv_connector.finish_forward
  hisparse.worker.finish_forward
  _enqueue_transfers
  _submit_dma_descriptors
  ops.swap_blocks_batch

RuntimeError: swap_blocks_batch, csrc/libtorch_stable/cache_kernels.cu:182,
cuMemcpyBatchAsync failed at index 15210 with error 1
```

The original run reached only 17/64 conversations. EvalScope may still exit
successfully after the engine dies, so treat any incomplete/failed requests as
a failed reproduction even if the client process returns zero.
