# GLM-5.3 blog 2 sweep

This is the canonical procedure for building comparable GLM-5.3 prefiller
Pareto curves on H200, B200, and GB200. It intentionally does not pin a vLLM
commit: each sweep selects a clean `ve` environment at runtime and records its
resolved commit with every result.

Use the short LMSYS OpenHands workload to build curves quickly. Run AgentX
afterward for long-duration validation of publication candidates.

The objective is to compare prefiller parallelism on an otherwise identical
P/D deployment, identify the throughput knee, and determine whether a change
improves both interactivity and aggregate logical throughput. This guide is a
protocol, not a status ledger: measured points and dated run state belong in
the artifact tree.

## Comparison matrix

Each accelerator directory under `manifesto/models/glm-5.3/` contains the
same three arms:

| Run name | Prefiller | Decoder |
| --- | --- | --- |
| `pcp8-ep8-mtp3` | PCP8 + EP8 | DP8 + EP8 |
| `pcp8-dcp8-ep8-a2a-mtp3` | PCP8 + DCP8 + EP8, A2A | DP8 + EP8 |
| `tp8-ep8-mtp3` | TP8 + EP8 | DP8 + EP8 |

Use the matching manifest paths:

```text
glm-5.3/<accelerator>/p1-pcp8ep-d1-dp8ep-mtp-agentx
glm-5.3/<accelerator>/p1-pcp8dcp8ep-d1-dp8ep-mtp-a2a-agentx
glm-5.3/<accelerator>/p1-tp8ep-d1-dp8ep-mtp-agentx
```

H200 and B200 use one eight-GPU node per role. GB200 uses the topology encoded
by its platform manifest. Never compare points that use different role sizes
or divide throughput by different serving-GPU counts.

## Fixed experiment settings

Keep these settings identical across all arms in one comparison:

- Model: `zai-org/GLM-5.3`.
- MTP3 on both prefiller and decoder.
- FP8 KV cache and prefix caching.
- Decoder DP8 + EP8 with CUDA graphs enabled.
- `gpu_memory_utilization=0.92`, unless one documented platform-wide value is
  required. Apply a changed value to every arm before comparing them.
- Prefiller `max_model_len=142000`, `max_num_batched_tokens=32768`, and
  `max_num_seqs=256` for the limited-context comparison.
- No `--cpu-offload-gb` and no `--enforce-eager`. Eager mode is permitted only
  as a debugging diagnostic and its measurements are not publication points.
- One instance-scoped llm-d router per concurrently deployed arm.

Before comparing results, render every arm and verify that all settings other
than the prefiller topology are equal. Do not silently retune one arm.

## Workload choice

The initial Pareto uses the LMSYS GLM OpenHands reproduction workload through
the repository's retained EvalScope client. It has fixed multi-turn agentic
request shapes and fixed output lengths, so a short c1/c2/c4/c8 sweep finishes
faster and has less request-drain variance than an open-loop AgentX run. The
dataset builder and client-library revisions are pinned inside
`reproductions/glm53-benchmarks/lmsys-client.sh`; the wrapper reuses their
generated dataset and dependency environment from `results/.artifacts/cache/`.

AgentX remains the final validation workload because it represents the Weka
trace population. Do not place LMSYS and AgentX points on one Pareto curve:
their request populations and cache behavior differ.

An SGLang control is valid only when it uses the same model weights, hardware,
serving-GPU count, generated dataset, request counts, and metric formulas. A
published NVFP4 result on different hardware is useful context, not a directly
comparable point for this FP8 vLLM curve.

## Select the build under test

Build or select a complete `ve` environment for the commit being tested. The
working tree must be clean:

```bash
: "${VLLM_ENV:?set VLLM_ENV to the ve environment under test}"
test -z "$(git -C "$VLLM_ENV" status --porcelain)"
VLLM_COMMIT="$(git -C "$VLLM_ENV" rev-parse HEAD)"
VLLM_COMMIT_SHORT="$(git -C "$VLLM_ENV" rev-parse --short=10 HEAD)"
printf 'Testing vLLM %s\n' "$VLLM_COMMIT"
```

Set the selected platform base's `runtime.vllm_env` and expected-build guard
to this environment and resolved commit. Update the platform base once; never
put different worker commits in individual arms. A source patch must first be
committed to a reproducible branch—do not benchmark a dirty checkout.

## Cluster environment

Use exactly one ignored runtime dotenv per cluster:

| Cluster | Runtime dotenv | Portable example |
| --- | --- | --- |
| H200 | `.env` | `.env.example` |
| B200 | `.env.b200` | `.env.b200.example` |
| GB200 | `.env.gb200` | `.env.gb200.example` |

Machine-local kubeconfig, SSH-key, cache, and workspace paths belong only in
the ignored runtime dotenv. Do not add them to examples, manifests, scripts,
or this guide.

For B200, select an arm after loading the shared cluster dotenv:

```bash
CONFIG=pcp8-ep8-mtp3
ENV_FILE=.env.b200
export ENV_FILE
set -a
source "$ENV_FILE"
source scripts/b200-glm53-config.sh "$CONFIG"
set +a
```

For H200 or GB200, load its cluster dotenv and set `CONFIG`, `MANIFESTO_SPEC`,
`MANIFESTO_USER`, `MODEL`, `ROUTER_RELEASE`, and `ROUTER_PROBE_PORT` for the
selected arm. Give concurrently running arms different owners, router
releases, and local ports.

## Create the run record

Every attempt gets a timestamped artifact root. The commit is part of the run
name, not hardcoded in this guide:

```bash
SWEEP_ID="$(date -u +%Y%m%dT%H%M%SZ)-${VLLM_COMMIT_SHORT}"
OUT="$PWD/results/.artifacts/reproductions/glm53-blog-2/$SWEEP_ID/$CONFIG"
LMSYS_CACHE_DIR="$PWD/results/.artifacts/cache/lmsys-glm-agentic"
mkdir -p "$OUT"

printf '%s\n' "$VLLM_COMMIT" > "$OUT/vllm-commit.txt"
git -C "$VLLM_ENV" status --porcelain=v1 > "$OUT/vllm-status.txt"
{
  printf 'CONFIG=%s\n' "$CONFIG"
  printf 'MANIFESTO_SPEC=%s\n' "$MANIFESTO_SPEC"
  printf 'MANIFESTO_USER=%s\n' "$MANIFESTO_USER"
  printf 'MODEL=%s\n' "$MODEL"
  printf 'ROUTER_RELEASE=%s\n' "$ROUTER_RELEASE"
  printf 'KUBE_CONTEXT=%s\n' "$KUBE_CONTEXT"
  printf 'NAMESPACE=%s\n' "$NAMESPACE"
} > "$OUT/run-config.env"
```

The empty `vllm-status.txt` proves the source tree was clean. Never write raw
results, logs, caches, datasets, virtual environments, or plots outside
`results/.artifacts/`.

## Deploy and capture the server configuration

```bash
just --dotenv-filename "$ENV_FILE" router
just --dotenv-filename "$ENV_FILE" args
just --dotenv-filename "$ENV_FILE" deploy
just --dotenv-filename "$ENV_FILE" render > "$OUT/manifest.yaml"
just --dotenv-filename "$ENV_FILE" args > "$OUT/vllm-args.yaml"
scripts/vllm-build-info.sh "llm-d.ai/owner=$MANIFESTO_USER" \
  > "$OUT/vllm-build.txt"
```

Record the exact rendered manifest and arguments for every arm even when only
the commit changed. Do not rely on a mutable branch name as the reproduction
record.

Capture startup logs and the resolved attention, linear, and MoE backends.
Backend defaults can change between commits, so never infer the active kernel
from a requested `auto` setting:

```bash
kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" logs \
  -l "llm-d.ai/owner=$MANIFESTO_USER" -c vllm --tail=-1 \
  > "$OUT/server-startup.log"
rg -i 'backend|kernel|flashinfer|deepgemm|triton|moe' \
  "$OUT/server-startup.log" > "$OUT/resolved-backends.log" || true
```

## Real P/D smoke gate

Run one conversation before starting a sweep:

```bash
OUTPUT_DIR="$OUT/smoke" \
LMSYS_CACHE_DIR="$LMSYS_CACHE_DIR" \
RUN_NAME="${CONFIG}-smoke-c1" \
LMSYS_PARALLELS=1 \
LMSYS_NUMBERS=1 \
INSTALL_DEPS=auto \
bash scripts/lmsys-run.sh

kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" logs \
  -l "llm-d.ai/owner=$MANIFESTO_USER,llm-d.ai/role=decode" \
  -c vllm --tail=-1 > "$OUT/decoder-smoke.log"
```

The smoke passes only when:

1. `Num successful transfers` is greater than zero.
2. `External prefix cache hit rate` reaches 100%.
3. The client finishes without request errors.
4. Neither role reports a traceback, connector error, KV-layer mismatch, or
   engine initialization failure.

```bash
rg 'Num successful transfers=[1-9]|External prefix cache hit rate' \
  "$OUT/decoder-smoke.log"
rg 'Number of KV layers must match|transfer_setup_failed|NIXL_ERR|Traceback|Engine core initialization failed' \
  "$OUT/decoder-smoke.log" || true
```

A 100% hit-rate line alone is insufficient: a failed transfer can still
produce a misleading hit-rate value.

## Fast Pareto sweep

The LMSYS OpenHands client runs fixed-shape agentic conversations at
concurrency `1,2,4,8`. Its dependency and dataset revisions are pinned inside
the retained client scripts and are recorded in the result metadata.

```bash
for point in 1:4 2:8 4:8 8:16; do
  concurrency="${point%%:*}"
  requests="${point##*:}"
  started="$(date -u +%Y%m%dT%H%M%SZ)"

  OUTPUT_DIR="$OUT/points" \
  LMSYS_CACHE_DIR="$LMSYS_CACHE_DIR" \
  RUN_NAME="${CONFIG}-c${concurrency}-${started}" \
  LMSYS_PARALLELS="$concurrency" \
  LMSYS_NUMBERS="$requests" \
  INSTALL_DEPS=0 \
  bash scripts/lmsys-run.sh

  kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" logs \
    -l "llm-d.ai/owner=$MANIFESTO_USER,llm-d.ai/role=decode" \
    -c vllm --since=30m > "$OUT/decoder-c${concurrency}-${started}.log"
done
```

If the curve is still improving after c8, add c16 and c32 using the same
procedure and record their request counts. Resume by running only missing
concurrencies. Delete only the incomplete concurrency directory containing
`benchmark_data.db-journal`; preserve all completed points.

## AgentX validation

Use AgentX for final long-duration validation, not initial curve discovery.
The limited run admits only requests whose peak context is at most 142,000
tokens; it does not truncate longer requests.

```bash
for concurrency in 1 8 32 64; do
  CONCURRENCY="$concurrency" \
  DURATION_SECONDS=1800 \
  AIPERF_RESULTS_MODE=pod \
  LOCAL_RESULTS_ROOT="$PWD/results/.artifacts" \
  ARTIFACT_SUBDIR="reproductions/glm53-blog-2/$SWEEP_ID/$CONFIG/agentx-c${concurrency}" \
  bash scripts/direct-run.sh
done
```

Use `semianalysis_cc_traces_weka_062126`, peak context at most 142,000, and no
truncation. A later full-context HiSparse experiment is a separate population
and must not be placed on the limited-context curve.

## Metrics and plotting

Report every completed point before computing a frontier:

- X axis, interactivity: `1000 / mean TPOT`, in output tokens/s/user.
- Y axis, aggregate logical throughput per serving GPU: prompt plus output
  tokens reported by the client, divided by all prefiller and decoder GPUs.
- Annotate concurrency, acceptance length, external prefix-cache hit rate,
  successful KV transfers, preemptions, and request errors.
- Logical prompt throughput includes prefix-cached prompt tokens; it is not
  physical GPU token computation.
- Request drain is part of the reported wall time. Keep request shape and
  count matched so its effect is comparable across arms.

Plot all valid points. A Pareto frontier may additionally omit a point only if
another point has both equal-or-higher interactivity and equal-or-higher
throughput. Perform plotting as post-processing from saved result files.

## Failure and retuning policy

Do not patch or retune during a publication sweep. Record the failure, make a
clean reproducible change, and restart the complete comparison matrix.

For a confirmed GPU-memory OOM:

1. Retune `gpu_memory_utilization` and apply the chosen value to every arm.
2. If still necessary, halve `max_num_batched_tokens` for every arm.
3. If still necessary, halve `max_num_seqs` for every arm.

Any point produced before a shared setting changes belongs to a different
curve. Never combine it with the retuned points.

## Operations and timing

B200 capacity may be spot-backed, so pods can disappear without indicating a
software regression. Keep completed client results in the artifact tree and
record pod restart count, node name, termination reason, and server logs for a
failed point before rescheduling it.

Model load and graph capture can take tens of minutes. Benchmark completion
also includes the final request-drain tail. Monitor every 5–15 minutes; do not
use a high-frequency polling loop. Run arms in parallel only when each has its
own prefiller, decoder, router, owner, client port, and output directory.

When estimating completion time, report startup, steady benchmark duration,
and drain separately. A long drain is workload behavior and must not be
silently removed from only one arm.

## Completion checklist

- Clean, resolved vLLM commit recorded.
- Exact rendered manifest and server arguments saved.
- Separate router and owner per concurrent arm.
- Real KV transfers and 100% external prefix-cache hits verified.
- c1, c2, c4, and c8 completed for all three arms.
- Optional c16/c32 captured consistently when needed.
- AgentX validation completed for publication candidates.
- Logs, raw results, metadata, and plots remain under
  `results/.artifacts/reproductions/glm53-blog-2/`.
