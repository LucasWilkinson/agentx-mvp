# GB200 GLM-5.3 PCP8 controls vs TP8 prefill

Tested on the GB200 cluster with eight-GPU prefill and eight-GPU decode P/D
stacks:

- PCP no-DCP arm: PCP8 + EP8 prefill, DP8 + EP8 MTP-3 decode.
- PCP DCP arm: PCP8 + DCP8 + EP8 prefill, DP8 + EP8 MTP-3 decode.
- TP arm: TP8 + EP8 prefill, DP8 + EP8 MTP-3 decode.
- vLLM environment HEAD: `215e61ea263656eded597609eba374cf23878970`
  (including the NIXL MTP registration fix); model revision
  `30333038ada1f1dacb294a93270305a890b50c14`.
- Workload: pinned LMSYS OpenHands GLM agentic trace, 13 turns per
  conversation, 74,173 prompt tokens on turn one, about 753 new tokens on
  later turns, and 220 fixed output tokens.
- Concurrency: 1, 2, 4, and 8 with 4, 8, 8, and 16 conversations.

The prefill-sensitive metric is first-turn TTFT. Aggregate TTFT also includes
the twelve prefix-reuse turns and decode, so it is not a direct prefiller
comparison.

## Throughput–interactivity Pareto

Interactivity is `1000 / mean ITL` in output tokens/s/user. The y-axis is
EvalScope's logical total-token throughput (prompt tokens, including cache
hits, plus output tokens) divided by the eight prefill chips.

![GB200 GLM-5.3 prefiller throughput–interactivity Pareto curves](gb200-glm53-prefill-paretos.svg)

Outlined points and solid segments are Pareto-efficient within each topology;
crossed points are dominated. The upper-right direction is better.

| Concurrency | Conversations | First-turn TTFT PCP-no-DCP / PCP-DCP / TP (s) | PCP-no-DCP vs TP | Later-turn TTFT PCP-no-DCP / PCP-DCP / TP (s) | Duration PCP-no-DCP / PCP-DCP / TP (s) | Output tok/s PCP-no-DCP / PCP-DCP / TP |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 4 | 13.153 / 16.583 / 17.576 | +25.17% | 3.128 / 3.698 / 3.507 | 1417.62 / 1430.74 / 1411.59 | 8.07 / 8.00 / 8.10 |
| 2 | 8 | 16.666 / 22.001 / 21.850 | +23.73% | 3.483 / 7.176 / 6.922 | 1543.17 / 1688.27 / 1682.37 | 14.83 / 13.55 / 13.60 |
| 4 | 8 | 21.726 / 35.694 / 38.322 | +43.31% | 3.344 / 8.955 / 8.136 | 860.57 / 953.89 / 969.65 | 26.59 / 23.99 / 23.60 |
| 8 | 16 | 35.651 / 82.898 / 90.452 | +60.59% | 6.942 / 67.962 / 69.726 | 897.26 / 2482.46 / 2546.57 | 51.00 / 18.43 / 17.97 |

Across all 36 cold first turns per arm, weighted by the number of
conversations, PCP-no-DCP averaged 25.838 seconds, PCP-DCP averaged 51.507
seconds, and TP averaged 55.525 seconds. PCP-no-DCP was 49.84% faster than
PCP-DCP and 53.47% faster than TP. All 468 requests per arm completed
successfully. Unlike the other arms, PCP-no-DCP continues scaling at c8: it
nearly doubles output throughput over c4 while processing twice as many
conversations in nearly the same duration.

## Interpretation

GLM-5.3 has 64 attention heads. TP8 leaves eight local heads per rank, but the
current FlashMLA path pads each rank to 64 heads. PCP keeps 64 heads and shards
the query sequence eight ways, avoiding most of that padded attention work.

Both PCP arms pay one cost that TP does not:

- MLA KV and KPE are all-gathered across PCP ranks for each layer in the tested
  branch.

PCP-DCP adds two more collectives:

- The DSA indexer performs an exact global merge by all-gathering candidates
  from all eight DCP ranks. GLM-5.3 selects 2,048 candidates.
- Attention outputs and LSE values use the DCP `ag_rs` merge.

Disabling DCP keeps the PCP query-sequence sharding but removes the DCP
candidate and attention-state merges. With the direct PCP KV path disabled,
each PCP rank instead operates on the gathered full KV/KPE state for its local
query rows. GB200 has enough memory for that replicated cache: the engine
reported capacity for about 880K KV-cache tokens at the configured memory
utilization.

The result identifies the lower DCP-enabled prefill service rate as the trigger
for the original arm's queueing knee. A subsequent [eight-rank c1/c8 torch
profile](gb200-glm53-dcp-profile.md) showed that DCP communication remains
about 9.5-9.8% of GPU-busy time rather than growing with concurrency. The 32K
batch-token cap admits only one full long-prompt chunk per step, so c8 mostly
queues behind an already 96-97% GPU-busy prefiller. Removing DCP also fixes the
roughly 753-token prefix-extension behavior: later-turn TTFT falls
substantially at every tested concurrency. The cold NIXL handoff is not the
bottleneck: the original layouts transferred about 3.8-4.0 GB in roughly 0.28
seconds; PCP exposes it as eight parallel shards while TP exposes one transfer.

For production, PCP8 + EP8 without DCP is the strongest tested prefill layout
for both large cold prompts and small cached extensions. The next useful
optimization is increasing its 32K batch-token limit, rather than restoring
DCP at this scale.

## Artifacts

Valid databases and summaries (the PCP-no-DCP directory contains c1-c8; the
other two contain c1-c4):

- `results/.artifacts/lmsys-glm-agentic/pcp8-ep8-nodcp-sparse-215e-clean/`
- `results/.artifacts/lmsys-glm-agentic/pcp8-dcp8-ep8-sparse-215e-clean3/`
- `results/.artifacts/lmsys-glm-agentic/tp8-ep8-sparse-215e-087-clean2/`

Valid clean c8 rerun, both started together with dataset offset 20 after a full
prefix-cache reset:

- `results/.artifacts/lmsys-glm-agentic/pcp8-dcp8-ep8-sparse-215e-c8-rerun-clean/`
- `results/.artifacts/lmsys-glm-agentic/tp8-ep8-sparse-215e-087-c8-rerun-clean/`

The PCP-no-DCP directory contains one uninterrupted c1-c8 sweep with fresh
dataset offsets `0, 4, 12, 20`. The first PCP-DCP/TP c8 attempt inside their
c1-c4 directories is invalid. Its shared SOCKS tunnel stopped forwarding and
both kubectl port-forwards logged `Timeout occurred`; no model failure was
involved.
