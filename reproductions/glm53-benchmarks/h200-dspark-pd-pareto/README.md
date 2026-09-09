# H200 GLM-5.3 DSpark7 P/D — throughput/interactivity Pareto

Charts and the aggregated data behind the 2026-09-08/09 prefiller comparison.

## Why this lives in the repo

`points.csv` is the **only surviving record** of six of its eight series. Their raw
sweep directories under `results/.artifacts/reproductions/` were deleted on
2026-09-09 during a disk cleanup, and `results/.artifacts/` is gitignored — so
everything here was one `rm` away from being unrecoverable. Treat `points.csv` as
source data, not as a derived artifact.

Only these series can still be re-collected from raw points:

- `20260909T140000Z-no54036-20a94597` — PR 54036 backed out
- `20260909T150000Z-simplify-a6f09074` — token-sharded DCP8 on the simplify branch
- `20260909T183000Z-nodk-1ca5131a` — no direct KV, **re-measured**

That last one is the cautionary tale. Its original sweep (`20260909T050000Z`) was
deleted after only TTFT / output tok/s / rps / acceptance had been written up by
hand. Both Pareto axes need `tpot_ms` and `total_tok_s_per_gpu`, neither of which
was transcribed, so the arm could not be plotted at all and had to be re-run on
the cluster. **Summarise a sweep by keeping its whole `points.csv` row, not the
subset that answers the current question.**

The re-run also shows how noisy the low-concurrency points are — same branch,
same env, same commit, hours apart:

| c | re-run TTFT | original TTFT | delta |
|---|---|---|---|
| 1 | 772.0 ms | 870.3 ms | -11.3% |
| 2 | 936.5 ms | 1062.2 ms | -11.8% |
| 4 | 7533.5 ms | 7529.6 ms | +0.1% |
| 8 | 15868.8 ms | 15967.6 ms | -0.6% |

c1/c2 move ~11% run to run while c4/c8 are stable to <1%. Any c1/c2 difference
smaller than that is noise, not signal.

## Scripts

| script | what it does |
|---|---|
| `collect_points.py` | Scans sweep dirs into `points.csv`. **Merge-based**: it seeds from the existing CSV and lets anything still on disk overwrite in place, so a missing sweep directory can never silently drop a series. Run from the repo root. |
| `plot_pareto.py` | Throughput-per-GPU vs interactivity, Pareto-efficient points outlined. Reads `points.csv`, writes `.svg` + `.png` beside itself. |
| `plot_ttft.py` | TTFT / request-rate / generation-throughput panels per concurrency. |
| `sheet_publish.py` | Publishes the 2026-09-08 comparison to the Google Sheet (tabs 0-2). Needs the sweep dirs for P99 TTFT, so it is now partly historical. |
| `sheet_publish_nodk.py` | Publishes the "No-direct-KV lineage" tab. Idempotent — deletes an existing tab of the same title first. |

Run them with the plotting venv:

```
results/.artifacts/tools/plot-venv/bin/python reproductions/glm53-benchmarks/h200-dspark-pd-pareto/plot_pareto.py
```

## Reading the chart

- **teal, PCP8+DCP8** — peer gather, reads context KV from DCP owners' symmetric
  memory. Best arm measured: 9,650 tok/s/GPU at c8, 2.0 s TTFT. Requires PCP direct KV.
- **red, PCP8+DCP8 token-sharded** — the same topology without direct KV. 5,464
  tok/s/GPU at c8. This is the cost of dropping direct KV.
- **blue/navy, PCP8 DCP1** — replicated KV pool; collapses at c4 once the pool saturates.
- **purple, TP8+EP8** — 82 s TTFT at c8 when the prefiller saturates.
- **orange** — the decoder prefilling itself, 8 GPUs, c1 only.

## Caveats carried in the data

- The symmetric-staging c1 point is not a measurement (6 requests, 4 failures, 27 s).
- The simplify arm's c8 acceptance reads 90.8% in evalscope, an estimator artifact:
  10.88 decoded tok/iter exceeds the 8-token ceiling for 7 speculative tokens.
  Use `../extract-vllm-spec-metrics.py`, which differences vLLM's Prometheus
  counters instead.
