set dotenv-load
set export

# AIPerf AgentX-MVP benchmark against the running llm-d optimized-baseline deployment.
#
# Usage:
#   just deploy            # apply the manifest and wait for the pod
#   just check             # confirm the runner can reach the model endpoint
#   just run               # run the full AgentX-MVP benchmark (default 1800s)
#   just run 16 900        # override concurrency / duration
#   just smoke             # fast plumbing test (~60s, marks result invalid)
#   just results           # copy artifacts out to ./results
#   just logs / just shell # inspect the runner
#   just clean             # delete the runner

# Let's take this from .env
# namespace := "llm-d-optimized-baseline"
NAMESPACE := env_var('NAMESPACE')
deploy    := "aiperf-agentx"
# model     := "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-FP8"
model     := "zai-org/GLM-5.2-FP8"
url       := "http://llm-d-inference-gateway-istio:80/v1"
# url       := "http://optimized-baseline-direct"
concurrency := "64"
duration    := "900"

pd_prefill := env_var('GLM_PD_PREFILL')
pd_decode  := env_var('GLM_PD_DECODE')
llm_d_root := env_var('LLM_D_ROOT')

default:
    @just --list

# Apply the manifest and wait for the runner pod to be ready.
deploy:
    kubectl apply -f agentx.yaml -n {{NAMESPACE}}
    kubectl rollout status deploy/{{deploy}} -n {{NAMESPACE}} --timeout=300s

# Sanity check: list models served through the llm-d router from inside the runner.
# (The slim image has no curl, so use python's urllib.)
check:
    kubectl exec -n {{NAMESPACE}} deploy/{{deploy}} -- \
      python -c "import urllib.request as u; print(u.urlopen('{{url}}/models', timeout=10).read().decode())"

# Send a single short request to warm up Triton JIT compilation (up to 10min timeout).
warmup:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Warming up model (this can take several minutes on first request)..."
    kubectl exec -n {{NAMESPACE}} deploy/{{deploy}} -- \
      python -c "
    import urllib.request, json
    req = urllib.request.Request('{{url}}/chat/completions',
        data=json.dumps({'model':'{{model}}','messages':[{'role':'user','content':'Hi'}],'max_tokens':8}).encode(),
        headers={'Content-Type':'application/json'})
    resp = urllib.request.urlopen(req, timeout=600).read().decode()
    print(resp)
    "
    echo "Warmup complete."

# Run the AgentX-MVP benchmark. Args: [concurrency] [duration-seconds].
# Launches detached inside the pod and polls for completion to survive kubectl connection drops.
run concurrency=concurrency duration=duration:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    DEPLOY={{deploy}}
    POD=$(kubectl get pod -n "$NS" -l app=$DEPLOY -o jsonpath='{.items[0].metadata.name}')
    # Kill any existing benchmark before launching a new one
    kubectl exec -n "$NS" "$POD" -- bash -c '
      for f in /proc/[0-9]*/cmdline; do
        pid=${f#/proc/}; pid=${pid%/cmdline}
        tr "\0" " " < "$f" 2>/dev/null | grep -q "aiperf" && kill -9 "$pid" 2>/dev/null
      done
      rm -f /workspace/aiperf.pid /workspace/aiperf.exit_code
    ' 2>/dev/null || true
    # Write benchmark script into the pod, then launch fully detached
    SCRIPT="#!/bin/bash
    aiperf profile \
      --scenario inferencex-agentx-mvp \
      --url '{{url}}' \
      --model '{{model}}' \
      --max-context-length 128000 \
      --endpoint-type chat \
      --streaming \
      --use-server-token-count \
      --public-dataset semianalysis_cc_traces_weka_with_subagents \
      --concurrency {{concurrency}} \
      --benchmark-duration {{duration}} \
      --output-artifact-dir /workspace/artifacts \
      --ui simple \
      > /workspace/aiperf.log 2>&1
    echo \$? > /workspace/aiperf.exit_code
    rm -f /workspace/aiperf.pid"
    kubectl exec -n "$NS" "$POD" -- bash -c "echo '$SCRIPT' > /workspace/run_benchmark.sh && chmod +x /workspace/run_benchmark.sh"
    kubectl exec -n "$NS" "$POD" -- bash -c 'nohup bash /workspace/run_benchmark.sh </dev/null >/dev/null 2>&1 & echo $! > /workspace/aiperf.pid && echo "Launched PID $!"'
    # Poll for completion — pid file is removed when the benchmark finishes
    echo "Benchmark running detached (polling every 30s)..."
    while kubectl exec -n "$NS" "$POD" -- test -f /workspace/aiperf.pid 2>/dev/null; do
        kubectl exec -n "$NS" "$POD" -- tail -1 /workspace/aiperf.log 2>/dev/null || true
        sleep 30
    done
    echo "Benchmark process finished."
    EXIT_CODE=$(kubectl exec -n "$NS" "$POD" -- cat /workspace/aiperf.exit_code 2>/dev/null || echo "1")
    kubectl exec -n "$NS" "$POD" -- tail -20 /workspace/aiperf.log || true
    if [ "$EXIT_CODE" != "0" ]; then
        echo "ERROR: Benchmark exited with code $EXIT_CODE"
        exit 1
    fi

# Fast plumbing validation (~60s). Uses --unsafe-override so it runs below the
# scenario's 900s minimum; result is marked submission_valid: false.
smoke:
    kubectl exec -n {{NAMESPACE}} deploy/{{deploy}} -- \
      aiperf profile \
        --scenario inferencex-agentx-mvp \
        --unsafe-override \
        --url {{url}} \
        --model {{model}} \
        --max-context-length 128000 \
        --endpoint-type chat \
        --streaming \
        --use-server-token-count \
        --public-dataset semianalysis_cc_traces_weka_with_subagents \
        --concurrency {{concurrency}} \
        --benchmark-duration {{duration}} \
        --output-artifact-dir /workspace/artifacts \
        --ui simple

# Copy benchmark artifacts out of the runner to a local directory (default ./results).
results dest="./results":
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "{{dest}}"
    POD=$(kubectl get pod -n {{NAMESPACE}} -l app={{deploy}} -o jsonpath='{.items[0].metadata.name}')
    # kubectl cp often exits non-zero due to a spurious tar stream error; retry individual files if needed
    kubectl cp {{NAMESPACE}}/${POD}:/workspace/artifacts "{{dest}}" 2>/dev/null || true
    # Verify the critical file arrived; if not, copy it directly
    if [ ! -f "{{dest}}/profile_export_aiperf.json" ]; then
        kubectl cp {{NAMESPACE}}/${POD}:/workspace/artifacts/profile_export_aiperf.json "{{dest}}/profile_export_aiperf.json" 2>/dev/null || true
    fi
    if [ ! -f "{{dest}}/profile_export.jsonl" ]; then
        kubectl cp {{NAMESPACE}}/${POD}:/workspace/artifacts/profile_export.jsonl "{{dest}}/profile_export.jsonl" 2>/dev/null || true
    fi
    if [ ! -f "{{dest}}/profile_export_aiperf.json" ]; then
        echo "ERROR: profile_export_aiperf.json not found after copy"
        exit 1
    fi

# Delete benchmark artifacts from the runner pod.
wipe:
    kubectl exec -n {{NAMESPACE}} deploy/{{deploy}} -- rm -rf /workspace/artifacts

logs:
    kubectl logs -n {{NAMESPACE}} deploy/{{deploy}} -f

shell:
    kubectl exec -it -n {{NAMESPACE}} deploy/{{deploy}} -- bash

clean:
    kubectl delete -f agentx.yaml --ignore-not-found

# Capture vllm version info from a running deployment into a directory.
vllm-version dest=".":
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    # Image tag
    kubectl get pod -n "$NS" -l llm-d.ai/role=prefill -o jsonpath='{.items[0].spec.containers[0].image}' > "{{dest}}/vllm_image.txt"
    echo "" >> "{{dest}}/vllm_image.txt"
    # vLLM version from prefill pod startup logs
    POD=$(kubectl get pod -n "$NS" -l llm-d.ai/role=prefill -o jsonpath='{.items[0].metadata.name}')
    kubectl logs -n "$NS" "$POD" --all-containers 2>/dev/null \
      | sed -n 's/.*version \([^ ]*\).*/\1/p' | head -1 > "{{dest}}/vllm_version.txt" || true
    echo "vllm version saved to {{dest}}/"
    cat "{{dest}}/vllm_image.txt"
    cat "{{dest}}/vllm_version.txt"

# Dump logs from all involved pods into a directory.
dump-logs dest=".":
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    mkdir -p "{{dest}}/logs"
    for pod in $(kubectl get pods -n "$NS" -l llm-d.ai/role -o jsonpath='{.items[*].metadata.name}'); do
        echo "  logs: $pod"
        kubectl logs -n "$NS" "$pod" --all-containers > "{{dest}}/logs/${pod}.log" 2>&1 || true
    done
    # EPP
    for pod in $(kubectl get pods -n "$NS" -l llm-d-router-gateway=wide-ep-lws-epp -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        echo "  logs: $pod"
        kubectl logs -n "$NS" "$pod" --all-containers > "{{dest}}/logs/${pod}.log" 2>&1 || true
    done
    # Gateway
    for pod in $(kubectl get pods -n "$NS" -l gateway.networking.k8s.io/gateway-name -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        echo "  logs: $pod"
        kubectl logs -n "$NS" "$pod" --all-containers > "{{dest}}/logs/${pod}.log" 2>&1 || true
    done
    # aiperf runner
    for pod in $(kubectl get pods -n "$NS" -l app={{deploy}} -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        echo "  logs: $pod"
        kubectl logs -n "$NS" "$pod" --all-containers > "{{dest}}/logs/${pod}.log" 2>&1 || true
    done
    echo "Logs saved to {{dest}}/logs/"

# Export Grafana dashboards for result directories.
# Usage: just scrape-grafana results_p1_d2_c1 results_p1_d2_c4
scrape-grafana +dirs:
    python3 export_dashboard.py results {{dirs}}

# Scrape Grafana dashboards and generate interactivity chart in one shot.
# Automatically finds all result directories containing profile_export_aiperf.json.
# Usage: just report results_routing
report outdir:
    #!/usr/bin/env bash
    set -euo pipefail
    DIRS=$(find "{{outdir}}" -name "profile_export_aiperf.json" -exec dirname {} \;)
    if [ -z "$DIRS" ]; then
        echo "No result directories found in {{outdir}}"
        exit 1
    fi
    python3 export_dashboard.py results $DIRS
    python3 gen_interactivity_chart.py "{{outdir}}"

# Deploy PD: just start-pd <prefill_replicas> <decode_size> [max_tokens]
# prefill_replicas = number of prefill pods
# decode_size = number of decode nodes (each node = 8 GPUs via EP)
# max_tokens = decode MAX_TOKENS env var (default 1024)
start-pd prefill_replicas decode_size:
    #!/usr/bin/env bash
    set -euo pipefail
    ROOT={{llm_d_root}}
    source "$ROOT/guides/env.sh"
    # Upgrade router with wide-ep-lws + GLM-5.2 overrides (sets all 8 DP ports)
    # Use non-dev chart (the -dev chart was pruned from the registry)
    CHART="oci://ghcr.io/llm-d/charts/llm-d-router-gateway"
    helm upgrade --install wide-ep-lws \
        "$CHART" \
        -f "$ROOT/guides/recipes/router/base.values.yaml" \
        -f "$ROOT/guides/recipes/router/features/httproute-flags.yaml" \
        -f "$ROOT/guides/wide-ep-lws/router/wide-ep-lws.values.yaml" \
        -f "$ROOT/guides/wide-ep-lws/router/glm-5.2-overrides.values.yaml" \
        --set provider.name=istio \
        --set router.epp.image.tag=v0.9.0 \
        -n {{NAMESPACE}} --version v0.9.0
    # Deploy prefill/decode LWS
    PREFILL_REPLICAS={{prefill_replicas}} envsubst '${PREFILL_REPLICAS}' < {{pd_prefill}} | kubectl apply -n {{NAMESPACE}} -f -
    DECODE_SIZE={{decode_size}} envsubst '${DECODE_SIZE}' < {{pd_decode}} | kubectl apply -n {{NAMESPACE}} -f -
    echo "Deployed P{{prefill_replicas}} D{{decode_size}} — waiting for pods..."
    kubectl rollout status --watch statefulset/wide-ep-lws-nvidia-gpu-vllm-glm-5-2-prefill -n {{NAMESPACE}} --timeout=2700s &
    kubectl rollout status --watch statefulset/wide-ep-lws-nvidia-gpu-vllm-glm-5-2-decode -n {{NAMESPACE}} --timeout=2700s &
    wait

# Tear down PD deployment.
stop-pd:
    kubectl delete lws wide-ep-lws-nvidia-gpu-vllm-glm-5-2-prefill wide-ep-lws-nvidia-gpu-vllm-glm-5-2-decode -n {{NAMESPACE}} --ignore-not-found

# Sweep concurrency (1..64 powers of 2) for the currently deployed P:D config.
# Results go to results_<prefix>_c<N>/ directories.
# Usage: just sweep-concurrency p2_d1
sweep-concurrency prefix="sweep" dest="." duration="900":
    #!/usr/bin/env bash
    set -euo pipefail
    for C in 1 4 16 64; do
        RDIR="{{dest}}/results_{{prefix}}_c${C}"
        if [ -f "$RDIR/profile_export_aiperf.json" ]; then
            echo "=== concurrency=$C — already exists, skipping ==="
            continue
        fi
        echo "=== concurrency=$C ({{duration}}s) ==="
        just warmup
        just run $C {{duration}}
        just results "$RDIR"
        just dump-logs "$RDIR"
        just wipe
        sleep 10
    done

# Full sweep: deploy each P:D config, run concurrency sweep, tear down.
# Configs are space-separated P:D pairs.
# Usage: just sweep results_glm52_run1 "1:2 2:1 2:2"
sweep outdir configs duration="900":
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    mkdir -p "{{outdir}}"
    just wipe
    FIRST=true
    for cfg in {{configs}}; do
        IFS=: read -r P D <<< "$cfg"
        dir="{{outdir}}/results_p${P}_d${D}"
        # Skip if all concurrency results already exist
        ALL_DONE=true
        for C in 1 4 16 64; do
            if [ ! -f "$dir/results_p${P}_d${D}_c${C}/profile_export_aiperf.json" ]; then
                ALL_DONE=false
                break
            fi
        done
        if [ "$ALL_DONE" = true ]; then
            echo "====== P${P}:D${D} — all concurrency levels done, skipping ======"
            continue
        fi
        echo "====== P${P}:D${D} ======"
        if [ "$FIRST" = true ]; then
            FIRST=false
        else
            just stop-pd
        fi
        just start-pd "$P" "$D"
        just check
        just warmup
        mkdir -p "$dir"
        kubectl get pod -n "$NS" -l llm-d.ai/role=prefill -o yaml > "$dir/prefill.yaml"
        kubectl get pod -n "$NS" -l llm-d.ai/role=decode -o yaml > "$dir/decode.yaml"
        kubectl get pod -n "$NS" -l llm-d-router-gateway=wide-ep-lws-epp -o yaml > "$dir/epp.yaml" 2>/dev/null || true
        kubectl get inferencepool -n "$NS" -o yaml > "$dir/inferencepool.yaml" 2>/dev/null || true
        kubectl get httproute -n "$NS" -o yaml > "$dir/httproute.yaml" 2>/dev/null || true
        kubectl get configmap wide-ep-lws-epp -n "$NS" -o yaml > "$dir/epp-config.yaml" 2>/dev/null || true
        just vllm-version "$dir"
        just sweep-concurrency "p${P}_d${D}" "$dir" {{duration}}
        just stop-pd
    done

    # Export Grafana dashboards for all result dirs
    RESULT_DIRS=$(find "{{outdir}}" -maxdepth 2 -name "profile_export_aiperf.json" -exec dirname {} \;)
    if [ -n "$RESULT_DIRS" ]; then
        echo "====== Scraping Grafana dashboards ======"
        if python3 export_dashboard.py results $RESULT_DIRS 2>/dev/null; then
            echo "====== Generating interactivity chart ======"
            python3 gen_interactivity_chart.py "{{outdir}}"
        else
            echo ""
            echo "WARNING: Grafana not reachable. Run manually when port-forward is up:"
            echo "  just scrape-grafana $RESULT_DIRS"
            echo "  python3 gen_interactivity_chart.py {{outdir}}"
        fi
    fi
