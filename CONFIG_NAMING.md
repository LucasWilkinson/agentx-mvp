# Benchmark configuration naming

Use lowercase, hyphen-separated segments. Names should read like the serving
topology, for example:

```text
pcp8-dcp8-ep8-a2a
pcp8-ep8
tp8-ep8
```

Only include enabled features and explicitly overridden settings. Absence
means the feature is disabled or the model/vLLM default is used.

## Segment order

Use segments in this order:

```text
<parallelism>-<expert-parallelism>-<transport>-<speculation>-<cache>-<limits>-<commit>
```

Parallelism segments themselves follow execution order. For example, PCP then
DCP then EP is `pcp8-dcp8-ep8`, not `ep8-dcp8-pcp8`.

## Segments

| Segment | Meaning | Example |
| --- | --- | --- |
| `tp<n>` | Tensor-parallel size | `tp8` |
| `dp<n>` | Data-parallel size | `dp8` |
| `pcp<n>` | Prefill-context-parallel size | `pcp8` |
| `dcp<n>` | Decode-context-parallel size | `dcp8` |
| `ep<n>` | Expert-parallel size | `ep8` |
| `a2a` | All-to-all PCP/DCP transport | `a2a` |
| `mtp<n>` | Number of speculative MTP tokens | `mtp3` |
| `hisparse<n>gib` | HiSparse host-pool capacity | `hisparse256gib` |
| `offload<n>gib` | Native KV-offload capacity | `offload272gib` |
| `mnb<n>k` | `max_num_batched_tokens` | `mnb32k` |
| `mns<n>` | `max_num_seqs` | `mns256` |
| `gu<n>` | `gpu_memory_utilization × 100` | `gu92` |
| `ml<n>k` | Explicit `max_model_len` | `ml142k` |
| `<sha>` | Short vLLM commit; always last | `abcdef1234` |

Use lowercase `gib` for binary capacities and lowercase `k` for token counts
expressed in thousands. Use exact values rather than rounding when rounding
would make two configurations ambiguous.

## Parallelism rules

Express expert parallelism as its own segment:

- TP8 with EP8 is `tp8-ep8`, not `tep8`.
- DP8 with EP8 is `dp8-ep8`, not `dep8`.
- PCP8 with DCP8 and EP8 over A2A is `pcp8-dcp8-ep8-a2a`.

This makes every enabled parallelism dimension explicit and keeps manifest,
router, result-directory, and plot labels consistent.

## Absence rules

- No `ep` segment means expert parallelism is disabled.
- No `mtp` segment means speculative MTP is disabled.
- No `hisparse` segment means HiSparse is disabled.
- No `offload` segment means native KV offloading is disabled.
- No `ml`, `mnb`, `mns`, or `gu` segment means the default is used.
- Do not write zero-valued segments such as `offload0gib`.

## Examples

The three GLM-5.3 prefiller arms are:

```text
pcp8-ep8-mtp3
pcp8-dcp8-ep8-a2a-mtp3
tp8-ep8-mtp3
```

Expanded result names may include cache and serving limits:

```text
tp8-ep8-mtp3-hisparse256gib-mnb32k-mns256-gu92-ml142k-abcdef1234
tp8-ep8-mtp3-offload272gib-mnb32k-mns256-gu92-ml142k-abcdef1234
```

Use the short topology name for stable configuration identifiers. Add limits
and the resolved commit to immutable run/result names when they vary across
sweeps.
