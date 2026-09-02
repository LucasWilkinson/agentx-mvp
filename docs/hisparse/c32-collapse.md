# GLM-5.3 HiSparse concurrency-collapse reproducer

## Finding

On 8× H200, this configuration drops sharply between concurrency 16 and 32:

```text
TP8_MTP3_HiSparse256GiB_MNB32K_MNS256_GU92_ML142K_530c874d64
```

| Concurrency | Requests/s | Total tokens/s/GPU | TTFT | TPOT |
| ---: | ---: | ---: | ---: | ---: |
| 16 | 0.8772 | 8,762.8 | 4.33 s | 62.57 ms |
| 32 | 0.1249 | 1,247.7 | 143.44 s | 499.01 ms |

The strongest explanation in the recorded metrics is loss of actual local
prefix-cache residency, not preemption or a lower logical-reuse workload:

| vLLM counter delta | c16 | c32 |
| --- | ---: | ---: |
| `prompt_tokens_by_source_total{source="local_cache_hit"}` | 30,037,184 | 336,960 |
| `prompt_tokens_by_source_total{source="local_compute"}` | 3,115,884 | 65,950,851 |
| Actual local-hit share | 90.6% | 0.5% |
| `hisparse_host_to_device_bytes_total` | 1.12 TB | 4.18 TB |
| `num_preemptions_total` | 0 | 0 |

At c32, nearly the entire prompt is recomputed. HiSparse host-to-device traffic
per request also roughly doubles.

## Why 256 GiB is not enough at c32

With single-node TP8 multiprocessing, HiSparse uses one shared host mmap across
all eight ranks. The model's replicated MLA KV is stored once, so the configured
256 GiB is the total retention capacity rather than 256 GiB per rank.

For this pinned GLM-5.3 revision, the 78 layers plus MTP layer consume about
51.8 KiB of FP8 DS-MLA host storage per token. The startup log confirms the
resulting allocation:

```text
255.9 GiB logical, 256.0 GiB physical, 82,850 blocks
```

At 64 tokens per host block, this retains exactly 5,302,400 prefix tokens.

|  | Live ceiling | Pool slack | Idle 142K contexts retainable |
| --- | ---: | ---: | ---: |
| c16 | 2.272M tokens | ~3.03M tokens | ~21 |
| c32 | 4.544M tokens | ~0.76M tokens | ~5 |

The c32 point contains 64 conversations but permits 32 to run concurrently.
Those live requests can reserve roughly 86% of the host pool. Blocks belonging
to conversations idle between turns sit on the free/LRU list, where allocations
for the other live requests evict them. Under the workload's cyclic access
pattern, the cache therefore thrashes and its hit rate falls off a cliff rather
than degrading gradually. The measured 0.5% local-hit share and quadrupled H2D
traffic are the direct consequences.

This is a capacity-and-replacement-policy boundary, not evidence of scheduler
preemption. A lower-level GPU profile would still be needed to apportion the
remaining time between recompute, host DMA, and scheduling.

EvalScope's approximately 92% `KV Cache Hit Rate` remains constant because it
describes the workload's logical prefix reuse. Use vLLM's
`prompt_tokens_by_source_total` counters to determine whether those logical
prefixes were actually served from the local cache.

## Requirements

- One machine with 8× NVIDIA H200 GPUs.
- vLLM built from
  `neuralmagic/vllm@530c874d64f1e3bcb1eefac57313b78b6db67929`.
- This `agentx-mvp` checkout.
- Model weights for `zai-org/GLM-5.3` revision
  `30333038ada1f1dacb294a93270305a890b50c14`.

No source patches or eager-mode overrides are used.

## Terminal 1: server

Run from the vLLM checkout:

```bash
export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
export NCCL_IB_HCA=ibp
export NCCL_SOCKET_IFNAME=eth0
export GLOO_SOCKET_IFNAME=eth0
export VLLM_SKIP_P2P_CHECK=1

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
  --attention-config '{"hisparse_config":{"host_pool_gib":256}}' \
  --speculative-config '{"method":"mtp","num_speculative_tokens":3}' \
  --enable-auto-tool-choice \
  --tool-call-parser glm47 \
  --reasoning-parser glm45
```

Expert parallelism is not enabled.

## Terminal 2: c16 then c32

Run from the `agentx-mvp` root:

```bash
BASE_URL=http://127.0.0.1:8000 \
SERVED_MODEL=glm-agentx \
TOKENIZER_MODEL=zai-org/GLM-5.3 \
OUTPUT_DIR="$PWD/results/.artifacts/reproductions/glm-5.3-30333038/c16-c32-collapse" \
RUN_NAME=tp8-mtp3-hisparse256-530c874d64 \
INSTALL_DEPS=1 \
LMSYS_DATASET_OFFSET=20 \
LMSYS_NUMBERS="32 64" \
LMSYS_PARALLELS="16 32" \
bash reproductions/glm53-benchmarks/lmsys-client.sh
```

`LMSYS_DATASET_OFFSET=20` preserves the same conversation allocation used by
the full c1/c8/c16/c32 sweep. The client runs c16 and c32 sequentially. It uses
the pinned LMSYS OpenHands reproduction and EvalScope revisions declared in the
script.

## Optional: preserve Prometheus evidence

Start this before the client:

```bash
METRICS_DIR="$PWD/results/.artifacts/reproductions/glm-5.3-30333038/c16-c32-collapse/metrics"
mkdir -p "$METRICS_DIR"
while sleep 30; do
  curl -sf http://127.0.0.1:8000/metrics \
    >"$METRICS_DIR/$(date -u +%Y%m%dT%H%M%SZ).prom"
done
```

Compare the first and last snapshots for each point using:

```bash
grep -E '^vllm:(prompt_tokens_by_source_total|hisparse_host_to_device_bytes_total|num_preemptions_total)' \
  results/.artifacts/reproductions/glm-5.3-30333038/c16-c32-collapse/metrics/*.prom
```

## Original result artifacts

```text
/workspace/vdptest/results/.artifacts/reproductions/glm-5.3-30333038/hisparse-dma-refactor-530c874d64/tp8-hisparse-no-native-530c874d64-20260901T181206Z/
```
