# AgentX-MVP Benchmark

AIPerf AgentX-MVP benchmark harness for llm-d/manifesto deployments with prefill/decode disaggregation.

## Prerequisites

- `kubectl` configured for your cluster
- `.env` file with:
  ```
  NAMESPACE=vllm
  MANIFESTO_ROOT=$HOME/code/llm-manifesto
  MODEL_SPEC=models/deepseek-v4/1P-EP8-1D-EP8.yaml
  MANIFESTO_CLUSTER=clusters/oci-gb200.yaml
  MANIFESTO_USER=$USER
  ```
- Grafana port-forwarded to `localhost:3001` (for dashboard export)

Defaults target `deepseek-ai/DeepSeek-V4-Pro` on the smallest GB200/NVL72 manifesto profile and use only the existing `vllm` namespace.

## Quick start

```bash
just setup           # configure monitoring and deploy the aiperf runner
just check           # verify the model endpoint is reachable
just run             # run benchmark (default: concurrency=64, duration=900s)
just run 16 900      # override concurrency / duration
just smoke           # fast plumbing test (~60s, marks result invalid)
just results         # copy artifacts to ./results
just logs            # tail runner logs
just shell           # shell into the runner
just clean           # delete the runner pod
```

## Model Deployment

```bash
just start-model     # deploy the llm-manifesto spec
just stop-model      # tear down the manifesto deployment
```

## Sweep

Run the benchmark across concurrency levels (`1`, `16`, `64`, `256`) for the configured manifesto spec:

```bash
just sweep results_deepseekv4_nvl72

# Custom duration (default 900s)
just sweep results_deepseekv4_nvl72 1200
```

Each sweep produces result directories like `results_$USER-wide-ep-1p-ep8-1d-ep8/results_$USER-wide-ep-1p-ep8-1d-ep8_c1/`, `results_$USER-wide-ep-1p-ep8-1d-ep8/results_$USER-wide-ep-1p-ep8-1d-ep8_c16/`, etc. Each run directory contains:
- `profile_export_aiperf.json` — benchmark metrics
- `profile_export.jsonl` — per-request data
- `prefill.yaml` / `decode.yaml` — pod specs at time of run
- `vllm_image.txt` — vLLM container image tag
- `vllm_fingerprint.txt` — vLLM `system_fingerprint` from the API

## Grafana dashboard export

Export Grafana dashboards for benchmark result directories. Automatically extracts the exact time range each run executed (from `profile_export_aiperf.json` timestamps) and queries Prometheus for that window.

```bash
# Export dashboards for specific result directories
just scrape-grafana results_$USER-wide-ep-1p-ep8-1d-ep8/results_$USER-wide-ep-1p-ep8-1d-ep8_c1

# Or use the script directly for a single time range
python3 export_dashboard.py single --start now-30m --end now -o report.html
```

Each result directory gets a self-contained `dashboard.html` with interactive Plotly charts mirroring the Grafana dashboard.

## Dashboard overlay / comparison

Overlay multiple dashboard exports onto the same charts for side-by-side comparison across concurrency levels. X-axis is rebased to relative time (seconds from start) so runs that happened at different absolute times align.

```bash
# Overlay three concurrency levels — auto-labeled from filenames
python3 overlay_dashboards.py results_$USER-wide-ep-1p-ep8-1d-ep8/results_$USER-wide-ep-1p-ep8-1d-ep8_c1/dashboard.html results_$USER-wide-ep-1p-ep8-1d-ep8/results_$USER-wide-ep-1p-ep8-1d-ep8_c16/dashboard.html

# Custom labels
python3 overlay_dashboards.py c1.html c4.html --label "concurrency=1" --label "concurrency=4"
```

Each concurrency level gets a distinct color across all panels.

## vLLM version capture

Capture the vLLM version from a running deployment:

```bash
just vllm-version results_$USER-wide-ep-1p-ep8-1d-ep8
```

This is called automatically during sweeps.
