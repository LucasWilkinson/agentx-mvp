# GB200 GLM-5.3 PCP8+DCP8 prefill profile

Captured on 2026-08-29 from the PCP8 + DCP8 + EP8 prefiller at vLLM worktree
HEAD `215e61ea263656eded597609eba374cf23878970`. The workload used unique
~82K-token prompts, `max_num_batched_tokens=32768`, and one output token.
Torch-profiler stacks, shapes, FLOP counting, and memory recording were
disabled. Each trace contains CPU and CUDA activity from one of the eight DCP
ranks.

## Result

The concurrency collapse is a queueing/scheduling effect, not DCP collective
latency growing with concurrency.

- A warmed c1 request processed 82,350 tokens in 20.72 seconds: 3,974 prompt
  tok/s.
- Two warmed c8 batches processed 659,676 tokens in 160.31 seconds and 659,255
  tokens in 156.16 seconds: 4,115 and 4,222 aggregate prompt tok/s.
- At c8, vLLM reported one or two running requests while up to six were
  waiting for capacity. KV-cache utilization remained below 2%, so the queue
  was not caused by cache exhaustion.
- The c1 trace contains 32,768-, 32,768-, and 16,814-token engine steps. The
  two full steps took 7.64-7.71 seconds and the tail took 4.16 seconds.
- The first eight c8 steps each scheduled exactly 32,768 total tokens and took
  7.65-7.81 seconds. Most held one context; a second context was admitted only
  when it fit in leftover batch capacity.

One long LMSYS first turn therefore fills the entire global scheduler token
budget. More client concurrency mostly adds requests behind a prefiller whose
single-request workload already keeps the GPUs 96-97% busy. DCP's slower
per-chunk service time pushes the end-to-end P/D system past its queueing knee,
which also delays the dependent short prefix-extension turns and starves the
unchanged decoder.

## Rank-averaged CUDA profile

| Metric | c1 window | c8 window |
|---|---:|---:|
| Profiled engine steps | 3 | 8 |
| Wall time | 19.566 s | 61.787 s |
| GPU busy | 18.896 s (96.57%) | 59.771 s (96.74%) |
| NCCL all-gather | 1.445 s (4,608 calls, 313.6 us/call) | 4.604 s (14,312 calls, 321.7 us/call) |
| NCCL reduce-scatter | 0.342 s (1,638 calls, 208.9 us/call) | 1.093 s (5,226 calls, 209.1 us/call) |
| DCP stable global top-k | 0.223 s (735 calls, 302.7 us/call) | 0.706 s (2,268 calls, 311.4 us/call) |
| DCP attention-output correction | 0.245 s (1,638 calls, 149.7 us/call) | 0.780 s (5,226 calls, 149.3 us/call) |
| TRT-LLM MoE combine | 5.240 s | 16.591 s |

Communication remains 9.5-9.8% of rank GPU-busy time in both windows. The
per-call all-gather latency rises only 2.6%, while reduce-scatter and attention
correction latency are unchanged. The larger c8 totals reflect more profiled
full-size engine steps, not a concurrency-dependent collective slowdown.

That 9.5-9.8% is a collective-kernel share, not the inclusive cost of enabling
DCP. At c1, the matched PCP-no-DCP first-turn TTFT was 13.153 seconds versus
16.583 seconds with DCP: DCP added 26.1% latency, or reduced the corresponding
single-request service rate by 20.7%. The additional cost also includes:

- the stable global top-k merge (0.223 seconds in the c1 trace), including
  candidate packing and localization around its collective;
- attention-output correction (0.245 seconds), which is separate from NCCL;
- packed query/top-k movement, token filtering, copies and transposes around
  the collectives;
- one device-to-host valid-width decision for each sparse-attention layer
  chunk, which serializes launches even though its copy kernel is small; and
- the different block-sharded FlashMLA execution geometry and lost opportunity
  to overlap work at each layer boundary.

The rank-0 profile also reports 1.499 seconds in `aten::copy_` CUDA work,
compared with 1.760 seconds in NCCL kernels. Not all copy work is DCP-specific,
so it cannot be assigned wholesale, but it demonstrates why NCCL duration
alone is an incomplete overhead bound. A matched no-DCP profile is required
for exact inclusive kernel attribution.

The token-scattered A2A experiment targets only the attention-output merge. In
the AG/RS trace that merge executes 1,638 LSE all-gathers, 1,638 output
reduce-scatters, and 1,638 correction kernels. Packed A2A replaces those three
operations per chunk with one output+LSE all-to-all plus pack/unpack kernels;
it does not remove the query/top-k gather or the DSA global top-k merge.

## Token-scattered A2A follow-up

Commit `5bb3d0d758` added an NCCL token-scattered A2A path for the PCP8+DCP8
FlashMLA sparse attention merge. It ran in an isolated `ve` environment and
manifest with `VLLM_USE_DIRECT_DCP_A2A=0`; the existing head-scattered direct
workspace does not support variable token chunks. A real P/D completion
returned HTTP 200 before the timing runs.

Uncached direct-prefiller timings used the same 24,000-random-word generator
and one-token output as the original profile:

| Run | Prompt tokens | Wall time | Prompt throughput |
|---|---:|---:|---:|
| A2A c1 seed 2000 | 82,446 | 19.12 s | 4,311 tok/s |
| A2A c1 seed 2001 | 82,397 | 19.46 s | 4,233 tok/s |
| A2A c1 seed 2002 | 82,386 | 19.02 s | 4,332 tok/s |
| A2A c8 seed 3000 | 659,682 | 155.74 s | 4,236 tok/s aggregate |

The three c1 runs average 19.20 seconds and 4,292 tok/s, an 8.0% service-rate
gain over the profiled AG/RS c1 result of 3,974 tok/s. The c8 result is only
1.6% above the two-run AG/RS mean of 4,169 tok/s (4,115 and 4,222 tok/s), and
only 0.3% above its faster run. Packed A2A therefore removes measurable c1
merge overhead but does not move the saturated c8 throughput ceiling.

Three fresh, exact 74,173-token LMSYS first-turn prompts completed directly on
the A2A prefiller in 14.49, 14.20, and 14.16 seconds (14.28-second mean). Full
P/D requests for three additional prompts took 20.74 seconds for the first
cold NIXL registration and 15.57 and 15.55 seconds after warmup. The warmed
15.56-second P/D result is below the earlier c1 first-turn TTFTs for both AG/RS
PCP-DCP (16.583 seconds) and TP8 (17.576 seconds), but remains above PCP-no-DCP
(13.153 seconds). These max-one-token probes establish the prefill ordering;
the A2A arm still needs a full 220-output-token LMSYS Pareto sweep before being
added to the end-to-end Pareto chart.

This narrows the remaining DCP cost to the query/top-k gather, global DSA
candidate merge, token filtering/copy work, per-chunk launch synchronization,
and block-sharded FlashMLA execution. It also confirms that the c8 latency
collapse is queueing amplification: A2A preserves aggregate service rate but
cannot admit another 82K context while `max_num_batched_tokens` is 32K.

## Query-replication validation

The sparse-MLA query-replication change from vLLM PR #53465 was backported onto
the Frankenstein DCP base as commit `b0d10ae22` on
`codex/frankenstein-dcp-qrep`. A TP8+DCP8+EP8 prefiller with PCP1 and
`VLLM_DCP_Q_REPLICATE=1` passed the targeted 11-test suite and served a real
HTTP request. This configuration uses the AG/RS DCP backend: the packed A2A
path is specifically for token-sharded PCP+DCP, while QRep replicates query
heads across context-parallel ranks.

Three exact 74,173-token LMSYS first-turn requests took 24.270, 22.001, and
21.817 seconds, a 22.696-second mean (3,268 aggregate prompt tok/s, or 408.5
tok/s/chip). The matched TP8 no-DCP request took about 17.576 seconds. QRep is
therefore functional on the backport, but it is not a prefill win for this
workload. Its intended benefit is the decode regime, where query replication
avoids moving very small query shards. With PCP8+TP1, the query heads are
already replicated; QRep cannot eliminate the token-row communication that
dominates the PCP prefill topology.

The largest kernels are the TRT-LLM one-sided MoE combine and its FP8/BF16
BMMs. DCP all-gather is visible and worth optimizing, but removing all measured
DCP communication alone cannot explain the end-to-end c8 collapse.

## VeloQ render

![Rank-0 VeloQ timeline](../results/.artifacts/profiles/gb200-glm53-dcp-20260829/veloq/timeline.svg)

The rank-0 traces were also queried with VeloQ v0.6.3. The plotted values are
VeloQ's one-second PyTorch timeline buckets; communication is part of the total
CUDA kernel time rather than an additional stacked category.

| VeloQ rank-0 metric | c1 | c8 |
|---|---:|---:|
| Trace span | 20.389 s | 61.790 s |
| Summed CUDA kernel duration | 18.973 s | 59.974 s |
| DCP collective duration | 1.839 s (9.69%) | 5.860 s (9.77%) |
| NCCL all-gather mean | 324.5 us | 333.0 us |
| NCCL reduce-scatter mean | 209.7 us | 209.3 us |

This independently preserves the earlier conclusion: c8 repeats the same
near-saturated compute plateaus and periodic collective bursts for longer; it
does not show collective time growing as a share of GPU work. The VeloQ means
above are for rank 0, while the earlier table is averaged across all eight
ranks.

VeloQ's experimental PyTorch backend emits structured timelines rather than
its NSys-only SVG timeline. The checked-in renderer converts those VeloQ JSON
buckets to the SVG above without additional Python dependencies.

## Artifacts

- [c1 eight-rank traces](../results/.artifacts/profiles/gb200-glm53-dcp-20260829/c1/)
- [c8 eight-rank traces](../results/.artifacts/profiles/gb200-glm53-dcp-20260829/c8/)
- [VeloQ c1 report](../results/.artifacts/profiles/gb200-glm53-dcp-20260829/veloq/c1/)
- [VeloQ c8 report](../results/.artifacts/profiles/gb200-glm53-dcp-20260829/veloq/c8/)
- [VeloQ timeline SVG](../results/.artifacts/profiles/gb200-glm53-dcp-20260829/veloq/timeline.svg)
- [VeloQ timeline renderer](../scripts/render-veloq-timeline.py)
- The one-off profiler manifest was removed after this analysis; the settings
  required to reproduce it are recorded above.
- [Trace summarizer](../scripts/profile-summary.py)

The same raw traces remain on the cluster at
`/mnt/lustre/lwilkinson/profiles/glm53-gb200-pcp8dcp8ep/`.

## Frankenstein backport validation

The unified `codex/frankenstein-dcp-token-a2a` branch initially deadlocked on
the non-divisible 8,637-token scheduler tail of the exact 74,173-token LMSYS
first turn. The fused-Q CuTeDSL/Triton implementation, PDL, and breakable CUDA
graphs were ruled out independently. The Frankenstein base was instead
routing every PCP-spanning-DCP prefill through the generic dense-MLA fallback;
that path never entered the padded token-sharded sparse-MQA A2A implementation.

Setting `attention_config.sparse_mla_force_mqa=True` restored the intended
token-sharded path. The exact request then completed with HTTP 200. After one
20.30-second cold graph/kernel trial, five fresh-cache steady-state trials took
14.935, 14.715, 14.953, 14.816, and 14.732 seconds: 14.830 seconds mean, 5,002
aggregate prompt tok/s, or 625.2 prompt tok/s/chip. This is 15.6% lower latency
and 18.5% higher throughput than the 17.576-second TP8 reference. It is 3.9%
slower than the earlier token-A2A worktree's 14.28-second mean, so the remaining
branch delta still merits profiling before the full Pareto sweep.

The four-node P/D LMSYS OpenHands sweep then completed without failures. The
deployment used this PCP8+DCP8+EP8 prefiller and the shared DP8+EP8 MTP-3
decoder; per-chip throughput below divides by all 16 serving GPUs.

| Concurrency | Successful requests | Total throughput | Throughput/chip | TTFT | TPOT |
|---:|---:|---:|---:|---:|---:|
| 1 | 52/52 | 3,001.6 tok/s | 187.6 tok/s/chip | 3.58 s | 105.1 ms |
| 2 | 104/104 | 5,624.1 tok/s | 351.5 tok/s/chip | 4.23 s | 106.9 ms |
| 4 | 104/104 | 10,509.2 tok/s | 656.8 tok/s/chip | 5.32 s | 106.7 ms |
| 8 | 208/208 | 18,625.4 tok/s | 1,164.1 tok/s/chip | 8.05 s | 108.0 ms |

Throughput continues scaling through concurrency 8 while TPOT stays nearly
flat. The interactivity cost appears primarily in TTFT, which grows from 3.58
to 8.05 seconds as the prefiller queues more concurrent long contexts.

## Clean 64K PCP control and matched DCP rerun

The fully GPU-resident comparison was repeated at a 65,536-token global
prefill batch. Both PCP arms used the same DP8 + EP8, MTP-3, graph-enabled
decoder. Every request completed and neither prefiller nor decoder reported a
preemption.

| Concurrency | PCP8 + EP8 | PCP8 + DCP8 + EP8 A2A | PCP gain over DCP | TP8 + EP8 (32K reference) |
|---:|---:|---:|---:|---:|
| 1 | 28,418 tok/s | 21,430 tok/s | 32.6% | 25,787 tok/s |
| 2 | 55,987 tok/s | 42,220 tok/s | 32.6% | 45,483 tok/s |
| 4 | 96,694 tok/s | 63,447 tok/s | 52.4% | 73,598 tok/s |
| 8 | 164,012 tok/s | 92,930 tok/s | 76.5% | 111,284 tok/s |

Raising DCP from 32K to 64K changed throughput by -4.2%, -2.2%, -0.6%, and
+0.3% at c1/c2/c4/c8. The scheduler cap was therefore not the cause of the DCP
gap. PCP without DCP is the clear Pareto winner, including a 47.4% throughput
gain over TP at c8.

The auxiliary metrics also rule out cache behavior and decoder speculation as
the primary explanation:

| Metric | PCP8 + EP8 | PCP8 + DCP8 + EP8 | TP8 + EP8 |
|---|---:|---:|---:|
| Real prefix hit rate | 91.716% | 91.448% | 91.737% |
| Aggregate MTP acceptance length | 2.708 | 2.565 | 2.540 |
| Accepted draft-token rate | 56.93% | 52.15% | 51.32% |
| Prefiller KV capacity | 696,433 tokens | 5,634,539 tokens | 1,242,931 tokens |

DCP has substantially more KV capacity, but this workload never exhausted the
smaller PCP cache and no arm preempted. PCP also had the best MTP acceptance,
so the decoder did not cause its advantage. The remaining DCP loss is on the
prefill path: global DSA candidate merging, query/top-k movement, output/LSE
correction, packing and filtering, synchronization, and less favorable
block-sharded FlashMLA execution. The earlier profile measured only about 10%
of GPU-busy time directly in collectives, but a 26% inclusive DCP latency
penalty after accounting for the surrounding work; the 64K end-to-end result
confirms that this overhead compounds as long prefills queue.

## Matched 64K torch profiles

Matched rank-by-rank torch profiles were captured around the same 82K-token
direct prompt, with a 65,536-token scheduler cap. The request therefore ran in
two engine steps in both arms. The prefiller configurations differed only by
DCP8 and its required token-sharded attention path.

| Per-rank mean (8 ranks) | PCP8 + EP8 | PCP8 + DCP8 + EP8 A2A | DCP delta |
|---|---:|---:|---:|
| Profile wall span | 2.051 s | 7.149 s | +5.098 s |
| Summed GPU-kernel time | 2.043 s | 6.300 s | +4.258 s |
| CUDA kernel launches | 7,416 | 44,986 | +37,570 |
| NCCL all-gather | 33 ms / 356 calls | 1,195 ms / 2,939 calls | +1,162 ms |
| NCCL send/recv | 0 | 451 ms / 1,638 calls | +451 ms |
| `aten::copy_` CUDA kernels | 120 ms / 462 calls | 1,511 ms / 11,013 calls | +1,391 ms |
| Sparse-attention kernel | 301 ms / 156 calls | 767 ms / 1,638 calls | +466 ms |
| Stable global top-k | 0 | 225 ms / 903 calls | +225 ms |
| DCP pack + unpack | 0 | 251 ms / 3,276 calls | +251 ms |

The result is consistent across all eight ranks: DCP wall spans differ by only
4 ms from fastest to slowest, so this is not one straggling rank or node.
Common model work is essentially unchanged: the two BMM families total 452 ms
without DCP and 451 ms with DCP, while fused-Q is 81.6 ms in both profiles.

### Root cause

The current PCP+DCP implementation sets
`TOKEN_SHARDED_ROWS_PER_RANK = 512`. For every sparse-attention layer it loops
over 512-row chunks and, for each chunk, gathers packed query/top-k data, makes
the gathered views contiguous, filters global top-k candidates, launches the
sparse kernel, gathers LSE, and performs the A2A output merge. The 82K prompt
produced 21 chunks over its two scheduler steps. With 78 sparse layers this is
exactly 1,638 sparse-attention and A2A iterations. PCP without DCP makes one
attention call per layer per engine step: 156 calls.

That 10.5x fragmentation is the dominant regression. Direct collective time
adds about 1.61 s, but it also induces 10,551 extra copy kernels (mostly
`.contiguous()` clones), 1,482 extra attention launches, and repeated global
top-k/pack/unpack work. These surrounding costs explain why the end-to-end loss
is much larger than the collective-only percentage in the earlier profile.
The DCP trace also uses FlashMLA's `decode::head64::flash_fwd_splitkv...`
specialization for these small gathered chunks, whereas the PCP control uses
the larger `fwd::head128::sparse_attn_fwd` prefill specialization.

The next high-value experiment is to raise the 512-row chunk limit (subject to
its transient-memory bound), then profile 1K, 2K, and 4K rows. A durable fix
should also eliminate the materialized contiguous slices and fuse or replace
the per-chunk query/top-k and output/LSE exchanges. Increasing
`max_num_batched_tokens` alone cannot help while the inner 512-row loop remains.

Raw traces are retained on Lustre at:

- `/mnt/lustre/lwilkinson/profiles/glm53-gb200-64k-matched/dcp-a2a`
- `/mnt/lustre/lwilkinson/profiles/glm53-gb200-64k-matched/pcp-only`

The profiled DCP source was detached at commit `b511b25ad455` and contains the
512-row constant above. Rank-0 copies are also under the ignored local
`results/.artifacts/profiles/gb200-glm53-64k-matched/` directory for immediate inspection.
