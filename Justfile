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

# Run the AgentX-MVP benchmark. Args: [concurrency] [duration-seconds].
run concurrency=concurrency duration=duration:
    kubectl exec -n {{NAMESPACE}} deploy/{{deploy}} -- \
      aiperf profile \
        --scenario inferencex-agentx-mvp \
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
    mkdir -p {{dest}}
    kubectl cp {{NAMESPACE}}/$(kubectl get pod -n {{NAMESPACE}} -l app={{deploy}} -o jsonpath='{.items[0].metadata.name}'):/workspace/artifacts {{dest}}

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
    # system_fingerprint from the API
    kubectl exec -n "$NS" deploy/{{deploy}} -- \
      python -c "import urllib.request,json;r=json.loads(urllib.request.urlopen('{{url}}/chat/completions',data=json.dumps({'model':'{{model}}','messages':[{'role':'user','content':'hi'}],'max_tokens':1}).encode(),timeout=30).read());print(r.get('system_fingerprint','unknown'))" \
      > "{{dest}}/vllm_fingerprint.txt" 2>/dev/null || true
    echo "vllm version saved to {{dest}}/"
    cat "{{dest}}/vllm_image.txt"
    cat "{{dest}}/vllm_fingerprint.txt"

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
    for pod in $(kubectl get pods -n "$NS" -l app.kubernetes.io/name=epp -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
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

# Deploy PD: just start-pd <prefill_replicas> <decode_size> [max_tokens]
# prefill_replicas = number of prefill pods
# decode_size = number of decode nodes (each node = 8 GPUs via EP)
# max_tokens = decode MAX_TOKENS env var (default 1024)
start-pd prefill_replicas decode_size:
    #!/usr/bin/env bash
    set -euo pipefail
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
sweep-concurrency prefix="sweep" duration="900":
    #!/usr/bin/env bash
    set -euo pipefail
    for C in 1 4 16 64; do
        echo "=== concurrency=$C ({{duration}}s) ==="
        just run $C {{duration}}
        just results "results_{{prefix}}_c${C}"
        just dump-logs "results_{{prefix}}_c${C}"
        just wipe
        sleep 10
    done

# Full sweep: deploy each P:D config, run concurrency sweep, tear down.
# Configs are space-separated P:D pairs.
# Usage: just sweep "1:2 2:1 2:2"
sweep configs duration="900":
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    for cfg in {{configs}}; do
        IFS=: read -r P D <<< "$cfg"
        echo "====== P${P}:D${D} ======"
        just start-pd "$P" "$D"
        just check
        dir="results_p${P}_d${D}"
        mkdir -p "$dir"
        kubectl get pod -n "$NS" -l llm-d.ai/role=prefill -o yaml > "$dir/prefill.yaml"
        kubectl get pod -n "$NS" -l llm-d.ai/role=decode -o yaml > "$dir/decode.yaml"
        just vllm-version "$dir"
        just sweep-concurrency "p${P}_d${D}" {{duration}}
        just stop-pd
    done
