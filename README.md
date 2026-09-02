# GLM-5.2 AgentX benchmark

A thin Kubernetes harness for benchmarking GLM-5.2 variants with AgentX and
monitoring vLLM prefill/decode deployments in Grafana.

## Setup

Requirements: `kubectl`, `helm`, `just`, `uv`, a sibling `llm-manifesto` checkout, and an RWX results PVC.

```bash
cp .env.example .env
# Edit every REPLACE_ME value.
```

The endpoint and model are inputs, so this works for FP8, MXFP4, and other
GLM-5.2 variants without embedding cluster-specific values.

## Deploy the model

Model servers are rendered by [llm-manifesto](https://github.com/neuralmagic/llm-manifesto)
from the catalog in `manifesto/` (`clusters/coreweave-h200.yaml`,
`clusters/oci-gb200.yaml`, `models/glm-5.2/{h200,gb200,b200}/`).
Clone it next to this repo (`MANIFESTO_ROOT`, default `../llm-manifesto`).

```bash
just bootstrap                          # once per namespace: hf-secret + pull secret on the default SA
just router                             # once: llm-d router (Envoy + EPP) from deploy/router-values.yaml
just deploy                             # MANIFESTO_SPEC from .env
just deploy glm-5.2/h200/p1-pcp8dcp8ep-d1-dp8ep-dspark
just render                             # show what would be applied
just teardown                           # delete the current spec's model servers (--all for everything)
```

The router (Envoy + endpoint picker, P/D scheduling profile) is Helm-managed and selects any
model server labelled `llm-d.ai/inferenceServing=true` owned by `MANIFESTO_USER`, so `URL`
stays `http://<ROUTER_RELEASE>-epp:80` across specs. Manifesto's own Gateway/EPP objects are
stripped from the render (`scripts/env.sh: render_model`). The explicit targets
are `glm-5.2/h200/p1-pcp8dcp8ep-d1-dp8ep-dspark` and the experimental
`glm-5.2/gb200/p1-dp2pcp4dcp4ep-d1-dp8ep-dspark`. GB200 and discrete B200
remain separate catalogs because their CPU, topology, and fabric differ.
Accelerator guards prevent rendering either against the wrong cluster. The unsuffixed H200 target
remains temporarily for the in-flight CoreWeave session; the catalog also
retains the DP8 correctness baseline and replicated-PCP fallback.

The InferencePool lists target ports 8000-8007 so every DP rank of a decode pod is an
endpoint. The EPP assumes every pod serves all of them unless told otherwise, so
`scripts/filter-render.py` annotates each pod with `llm-d.ai/active-ports` (its declared
ports in that range: `8000` for single-rank pods, `8000,...,8007` for DP8 decode).

Known gap: manifesto renders the decode routing sidecar with fixed flags, so the reference
run's `--enable-prefiller-sampling` (used with MTP) is not applied. Add it upstream in
llm-manifesto if the smoke check shows it matters.

## Benchmark any vLLM build

vLLM itself is not baked into an image. Pods run the `vllm-envs` CUDA toolchain image and
activate a [`ve`](../vllm-envs) environment (a git worktree + `.venv`) from the workspace
PVC, selected with `VLLM_ENV`. To benchmark a branch, PR, or commit:

```bash
../devbox.sh sh                                  # zero-GPU box with the PVC and `ve`
ve new pr-52779 --name pcp-producers             # env under /workspace/envs/<name>
exit
just envs                                        # list envs and their HEADs
VLLM_ENV=/workspace/envs/pcp-producers just deploy
VLLM_ENV=/workspace/envs/pcp-producers just sweep pcp-producers
```

`just deploy` prints the vLLM version and commit actually loaded; sweeps record it in
`results_<config>/vllm_env.txt` (env path, commit, branch, dirty files) next to
`vllm_image.txt`. `VLLM_IMAGE` overrides the toolchain image when the Dockerfile changes.

## Sweeps

```bash
just sweep isl142k                                  # SWEEP_SPECS x SWEEP_CONCURRENCIES
just sweep dspark glm-5.2/h200/p1-pcp8dcp8ep-d1-dp8ep-dspark  # explicit spec
just results && just dashboard isl142k              # results/.artifacts/sweeps/isl142k/interactivity_vs_throughput.html
```

Each config gets `results_<config>/` containing `manifest.yaml`, `spec.yaml`, `config_name.txt`,
`pods.txt`, `model_label.txt`, `vllm_image.txt`, pod `logs/`, and one `results_<config>_c<N>/`
per concurrency. `gen_interactivity_chart.py` and `export_dashboard.py` read this layout directly.

Set `BENCHMARK_WORKLOAD=lmsys` to build initial Pareto curves with the pinned
LMSYS OpenHands workload instead. It runs fresh conversations at parallel
`1,2,4,8`; each conversation has 13 turns, a 74,160-token first turn,
753-token subsequent turns, and fixed 220-token outputs:

```bash
BENCHMARK_WORKLOAD=lmsys just sweep glm53-gb200-prefiller-comparison
```

The LMSYS client runs locally through a temporary router port-forward and writes
under each config's `lmsys/` directory. Dependency installation is automatic on
the first run and reused afterward. Run one already-deployed configuration with
`just lmsys tp8-none`, or invoke the reproduction client directly with explicit
`BASE_URL`, `SERVED_MODEL`, `TOKENIZER_MODEL`, `OUTPUT_DIR`, and `RUN_NAME`.

The workload follows the [LMSYS GLM optimization benchmark](https://www.lmsys.org/blog/2026-07-13-glm52-optimization).
Its [official reproduction branch](https://github.com/Jiminator/sglang/tree/glm-nvfp4-blog-repro/benchmark/glm_nvfp4_blog)
is pinned to `2bac7e166a7b5bf518b778817ec464cec0f75e3e`, and EvalScope is pinned to
`acd09b44384d53174768bb1063f675420f76fae9`. Use this controlled workload for
fast Pareto construction and AgentX's 1,800-second runs for final validation.

## Benchmark

```bash
just benchmark
just status
just logs
```

This submits a Kubernetes Job using the same publication profile as
`reproductions/glm53-benchmarks/agentx.sh`: the
`semianalysis_cc_traces_weka_062126` dataset, limited 142,000-token
population, 1,800-second duration, seed `20260827`, and pinned tokenizer
revision. Runs shorter than 900 seconds are automatically marked exploratory
with AIPerf's unsafe override. The job refuses to start when its context cap
exceeds the model's advertised `max_model_len`. Artifacts, including the exact
`agentx-config.env`, are durable on `RESULTS_PVC`.

The default baseline sweep probes concurrency `1 8 32 64`. Override
`SWEEP_CONCURRENCIES` or select `CONTEXT_PROFILE=full` only when the deployment
has the corresponding capacity.

## GB200 cluster access

GB200 is available as an explicit, context-safe operational target. See
[`docs/gb200-cluster.md`](docs/gb200-cluster.md) for the handoff and use
`just gb200-tunnel` plus `just gb200-status`. These commands always query
`--context default`; they do not switch away from the active CoreWeave context.
Use `.env.gb200.example` as the separate deployment configuration so the
CoreWeave `.env` remains untouched.

```bash
cp .env.gb200.example .env.gb200
# Fill in the arm64/CUDA 13 VLLM_ENV and VLLM_IMAGE values first.
just --dotenv-filename .env.gb200 render
```

## Live Grafana

Install a small namespace-local Prometheus and Grafana stack once:

```bash
just monitoring
just monitor
```

Open <http://127.0.0.1:3000/d/wideep-overview>. Prometheus scrapes every GLM
prefill/decode rank. The dashboard is provisioned automatically.

## Download and share results

```bash
just results
just dashboard
just grafana-export
```

`just dashboard <sweep>` creates `results/.artifacts/sweeps/<sweep>/interactivity_vs_throughput.html`, a
self-contained comparison of the downloaded runs. `just grafana-export`
embeds the matching Grafana time range into `dashboard.html` beside each run.

| Command | Purpose |
|---|---|
| `just bootstrap` | Namespace prerequisites (hf-secret, pull secret) |
| `just router` | Install/upgrade the llm-d router |
| `just envs` | List `ve` vLLM builds on the PVC |
| `just deploy [spec]` | Deploy a manifesto spec (vLLM from `VLLM_ENV`) and wait for the router to serve it |
| `just teardown [spec]` | Delete model servers |
| `just sweep <name> [specs]` | Deploy + benchmark each spec at every concurrency |
| `just benchmark` | Run AgentX against the configured GLM-5.2 model |
| `just lmsys [run-name]` | Run the LMSYS OpenHands 1,2,4,8 Pareto workload |
| `just status` | List runs and show the latest log command |
| `just gb200-tunnel` | Open the GB200 SOCKS proxy without changing kube context |
| `just gb200-status` | Show GB200 node/GPU allocation and `vllm` pods |
| `just logs` | Follow the newest benchmark run |
| `just monitoring` | Install/update Prometheus, Grafana, and dashboard |
| `just monitor` | Port-forward Grafana |
| `just results` | Download artifacts from the PVC |
| `just dashboard <sweep>` | Build a shareable HTML comparison of a sweep |
| `just grafana-export` | Export each run's Grafana dashboard to HTML |
| `just test` | Validate scripts and dashboard JSON |

## Kueue (optional queueing)

`just kueue install` installs the Kueue controller cluster-wide (`kueue-system`, chart
`registry.k8s.io/kueue` `KUEUE_CHART_VERSION`, values in `kueue/values.yaml`), the `h200`
ResourceFlavor, the `agentx` ClusterQueue (StrictFIFO, 32 H200 quota — edit `kueue/cluster-queue.yaml`),
and the `agentx` LocalQueue in `$NAMESPACE`. Only workloads carrying `kueue.x-k8s.io/queue-name` are
managed, so other namespaces are unaffected unless they opt in.

Set `KUEUE_QUEUE=agentx` in `.env` and every render (`just deploy`/`just sweep`) labels the model-server
Deployments/LeaderWorkerSets, and every aiperf Job is created `suspend: true` with the label; Kueue admits
them in FIFO order when the queue's quota covers them. Two sweeps started at once therefore run back to back
instead of fighting for GPUs. Caveat: Kueue only accounts *its own* workloads — GPUs held by bare pods in
other namespaces are invisible to it, so quota is a policy, not a measurement. While a deploy sits in the
queue `just deploy` keeps waiting for readiness (`MODEL_READY_TIMEOUT`, default 30m). `just kueue status`
lists queues and pending workloads; `just kueue uninstall` removes everything.

## Per-role vLLM builds (branches)

`VLLM_ENV` in `.env` is the default build for every role (rendered as `--vllm-env $VLLM_ENV`, a `ve` worktree with
its `.venv`). A spec can pin a different `ve` env for one role by setting that role's `env.MANIFESTO_VLLM_ENV` — the
launch script activates `$MANIFESTO_VLLM_ENV/.venv`, and role `env` is layered after the global default. Build a new env
on the devbox with `VE_CACHE_DIR=/workspace/.cache/vllm-envs VE_ENVS_ROOT=/workspace/vdptest ve new <remote>/<branch> --name <name> --repo /workspace/vdptest/vllm-main`
(the worktree must live on the workspace PVC so GPU pods can mount it).

Branch-specific args belong in the variant spec:
`extends` deep-merges mappings but *replaces* lists, so restate `vllm_raw_args` to drop a parent flag, and use
`key: $delete` to remove a mapping entry. `vllm_env.txt` in each sweep config dir records env, commit and branch
per role.

## PCP x DCP prefill

The H200 target runs PCP8 x DCP8 prefill on one 8-GPU node. Its GB200
analogue runs DP2 x PCP4 x DCP4 + EP8 across two 4-GPU nodes because PCP is
node-local in Manifesto; both decode with DP8 + EP. DCP block-shards KV within
each PCP group and publishes its shards to the decoder over NIXL. H200 pins the
proven CoreWeave build under `/workspace`; GB200 uses the arm64/CUDA 13
`VLLM_ENV` and `VLLM_IMAGE` selected in `.env.gb200`.
