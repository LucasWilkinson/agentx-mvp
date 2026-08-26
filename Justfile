set dotenv-load
set export

default:
    @just --list

# One-time namespace prerequisites (HF token secret, image pull secret on default SA).
bootstrap:
    scripts/bootstrap.sh

# Install/upgrade the llm-d router (Envoy + EPP) from deploy/router-values.yaml.
router:
    scripts/router.sh

# Render the model-server manifests for a spec (default: MANIFESTO_SPEC).
render spec="":
    scripts/render-model.sh {{spec}}

# Show the effective vLLM server args per role as readable YAML (default: MANIFESTO_SPEC).
args spec="":
    scripts/vllm-args.sh {{spec}}

# List vLLM builds (`ve` envs) on the workspace PVC; pick one with VLLM_ENV=<path>.
envs:
    scripts/envs.sh

# Deploy a spec with llm-manifesto (vLLM build from VLLM_ENV) and wait until the router serves MODEL.
deploy spec="":
    scripts/deploy-model.sh {{spec}}

# Delete model servers for a spec (default: MANIFESTO_SPEC); pass --all to delete every instance.
teardown spec="":
    scripts/teardown-model.sh {{spec}}

# Run AgentX once against URL at CONCURRENCY for DURATION_SECONDS.
benchmark:
    scripts/direct-run.sh

# Deploy each spec in turn and benchmark every SWEEP_CONCURRENCIES; results under <RESULTS_PREFIX>/<name>/.
sweep name +specs="":
    scripts/sweep.sh {{name}} {{specs}}

# Kueue (cluster-wide): install controller + h200 flavor + `agentx` queues; then set KUEUE_QUEUE=agentx in .env.
kueue action="status":
    scripts/kueue.sh {{action}}

# Install/update namespace-local Prometheus, Grafana, and the GLM dashboard.
monitoring:
    scripts/setup-monitoring.sh

# Open a local port-forward to Grafana.
monitor:
    scripts/grafana.sh

# Accuracy smoke check (lm_eval gsm8k, 5-shot, ACCURACY_LIMIT samples) against the served model: just accuracy [out-dir]
accuracy out="":
    scripts/accuracy-check.sh {{out}}

# List benchmark Jobs.
status:
    scripts/status.sh

# Follow the newest benchmark Job.
logs:
    scripts/logs.sh

# Download AgentX artifacts from the PVC into results/.
results:
    scripts/download-results.sh

# Build interactivity-vs-throughput HTML for a downloaded sweep: just dashboard <sweep-name>
dashboard sweep: results grafana-export
    python3 scripts/build-dashboard.py results/{{sweep}}

# Export the matching Grafana time window beside each downloaded run.
grafana-export:
    scripts/export-grafana.sh

# Validate scripts, specs, and dashboard JSON.
test:
    bash -n scripts/*.sh
    python3 -m json.tool dashboards/grafana-wideep-overview.json >/dev/null
    python3 -m py_compile scripts/build-dashboard.py scripts/filter-render.py scripts/vllm-args.py scripts/kv-cache-info.py scripts/run-context.py; bash -n scripts/vllm-build-info.sh export_dashboard.py gen_interactivity_chart.py
    scripts/validate-specs.sh
