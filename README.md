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

Model servers are rendered by [llm-manifesto](https://github.com/tlrmchlsmth/llm-manifesto)
from the catalog in `manifesto/` (`clusters/coreweave-h200.yaml`, `models/glm-5.2/*.yaml`).
Clone it next to this repo (`MANIFESTO_ROOT`, default `../llm-manifesto`).

```bash
just bootstrap                          # once per namespace: hf-secret + pull secret on the default SA
just router                             # once: llm-d router (Envoy + EPP) from deploy/router-values.yaml
just deploy                             # MANIFESTO_SPEC from .env
just deploy glm-5.2/p1-tp8ep-d1-dp8ep   # or any spec name
just render                             # show what would be applied
just teardown                           # delete the current spec's model servers (--all for everything)
```

The router (Envoy + endpoint picker, P/D scheduling profile) is Helm-managed and selects any
model server labelled `llm-d.ai/inferenceServing=true` owned by `MANIFESTO_USER`, so `URL`
stays `http://<ROUTER_RELEASE>-epp:80` across specs. Manifesto's own Gateway/EPP objects are
stripped from the render (`scripts/env.sh: render_model`). New variants are new files under
`manifesto/models/glm-5.2/` that `extends: base.yaml`.

Specs:

| Spec | What it is |
|---|---|
| `glm-5.2/p1-tp8-d1-dp8ep` | Dev checkout (`pr8-cv2`): prefill TP8, decode DP8+EP |
| `glm-5.2/p1-tp8ep-d1-dp8ep` | Same with EP on the prefill |
| `glm-5.2/ref-p1w1-d1w1-mtp-offload` | The llm-d "142k + MTP + Offloading" reference config (MTP-3, NIXL + CPU KV offload, DeepEP) shrunk to 1 prefill + 1 decode pod; needs a `ve` env with DeepEP/NVSHMEM kernels |

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
just sweep mtp glm-5.2/p1-tp8-d1-dp8ep              # explicit specs
just results && just dashboard isl142k              # results/isl142k/interactivity_vs_throughput.html
```

Each config gets `results_<config>/` containing `manifest.yaml`, `spec.yaml`, `config_name.txt`,
`pods.txt`, `model_label.txt`, `vllm_image.txt`, pod `logs/`, and one `results_<config>_c<N>/`
per concurrency. `gen_interactivity_chart.py` and `export_dashboard.py` read this layout directly.

## Benchmark

```bash
just benchmark
just status
just logs
```

This submits a Kubernetes Job running `inferencex-agentx-mvp` with the subagent
trace dataset. Change `CONCURRENCY`, `DURATION_SECONDS`, or `MODEL` in `.env`
between runs. Artifacts are durable on `RESULTS_PVC`.

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

`just dashboard <sweep>` creates `results/<sweep>/interactivity_vs_throughput.html`, a
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
| `just status` | List runs and show the latest log command |
| `just logs` | Follow the newest benchmark run |
| `just monitoring` | Install/update Prometheus, Grafana, and dashboard |
| `just monitor` | Port-forward Grafana |
| `just results` | Download artifacts from the PVC |
| `just dashboard <sweep>` | Build a shareable HTML comparison of a sweep |
| `just grafana-export` | Export each run's Grafana dashboard to HTML |
| `just test` | Validate scripts and dashboard JSON |
