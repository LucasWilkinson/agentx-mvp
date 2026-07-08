set dotenv-load
set export

# AIPerf AgentX-MVP benchmark against a manifesto-managed llm-d deployment.
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

NAMESPACE := env_var_or_default('NAMESPACE', 'vllm')
deploy    := "aiperf-agentx"
home := env_var_or_default('HOME', '')
manifesto_root := env_var_or_default('MANIFESTO_ROOT', home + '/code/llm-manifesto')
manifesto_spec := env_var_or_default('MODEL_SPEC', 'models/deepseek-v4/1P-EP8-1D-EP8.yaml')
manifesto_cluster := env_var_or_default('MANIFESTO_CLUSTER', 'clusters/oci-gb200.yaml')
manifesto_user := env_var_or_default('MANIFESTO_USER', env_var_or_default('USER', 'dev'))
manifesto_args := env_var_or_default('MANIFESTO_ARGS', '')
model     := env_var_or_default('MODEL', 'deepseek-ai/DeepSeek-V4-Pro')
model_label := env_var_or_default('MODEL_LABEL', 'DeepSeek-V4-Pro')
max_context_length := env_var_or_default('MAX_CONTEXT_LENGTH', '128000')
url       := env_var_or_default('URL', '')
concurrency := "64"
duration    := "900"

default:
    @just --list

# Apply the manifest and wait for the runner pod to be ready.
deploy:
    kubectl apply -f agentx.yaml -n {{NAMESPACE}}
    kubectl rollout status deploy/{{deploy}} -n {{NAMESPACE}} --timeout=300s

# Sanity check: list models served through the llm-d router from inside the runner.
# (The slim image has no curl, so use python's urllib.)
check:
    #!/usr/bin/env bash
    set -euo pipefail
    URL=$(just --quiet _model-url)
    kubectl exec -n {{NAMESPACE}} deploy/{{deploy}} -- \
      env URL="$URL" python -c "import os, urllib.request as u; print(u.urlopen(os.environ['URL'] + '/models', timeout=10).read().decode())"

# Send a single short request to warm up Triton JIT compilation (up to 10min timeout).
warmup:
    #!/usr/bin/env bash
    set -euo pipefail
    URL=$(just --quiet _model-url)
    echo "Warming up model (this can take several minutes on first request)..."
    attempt=0
    while true; do
        attempt=$((attempt + 1))
        if kubectl exec -n {{NAMESPACE}} deploy/{{deploy}} -- \
          env URL="$URL" MODEL="{{model}}" python -c "
    import os, urllib.request, json
    req = urllib.request.Request(os.environ['URL'] + '/chat/completions',
        data=json.dumps({'model': os.environ['MODEL'], 'messages':[{'role':'user','content':'Hi'}],'max_tokens':8}).encode(),
        headers={'Content-Type':'application/json'})
    resp = urllib.request.urlopen(req, timeout=600).read().decode()
    print(resp)
    "; then
            echo "Warmup complete."
            break
        fi
        echo "Warmup attempt $attempt failed, retrying in 30s..."
        sleep 30
    done

# Run the AgentX-MVP benchmark. Args: [concurrency] [duration-seconds].
# Launches detached inside the pod and polls for completion to survive kubectl connection drops.
run concurrency=concurrency duration=duration:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    DEPLOY={{deploy}}
    URL=$(just --quiet _model-url)
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
      --url '$URL' \
      --model '{{model}}' \
      --max-context-length {{max_context_length}} \
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
    #!/usr/bin/env bash
    set -euo pipefail
    URL=$(just --quiet _model-url)
    kubectl exec -n {{NAMESPACE}} deploy/{{deploy}} -- \
      aiperf profile \
        --scenario inferencex-agentx-mvp \
        --unsafe-override \
        --url "$URL" \
        --model {{model}} \
        --max-context-length {{max_context_length}} \
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

# Wait for all running requests to drain on prefill and decode pods.
drain:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    echo "Waiting for all requests to drain..."
    while true; do
        TOTAL=0
        for pod in $(kubectl get pods -n "$NS" -l llm-d.ai/role=prefill -o jsonpath='{.items[*].metadata.name}'); do
            for port in 8000 8001 8002 8003 8004 8005 8006 8007; do
                N=$(kubectl exec -n "$NS" "$pod" -c vllm -- \
                    curl -sf "http://localhost:${port}/metrics" 2>/dev/null \
                    | grep '^vllm:num_requests_running' | awk '{printf "%d", $2}') || N=0
                TOTAL=$((TOTAL + N))
            done
        done
        for pod in $(kubectl get pods -n "$NS" -l llm-d.ai/role=decode -o jsonpath='{.items[*].metadata.name}'); do
            for port in 8200 8201 8202 8203 8204 8205 8206 8207; do
                N=$(kubectl exec -n "$NS" "$pod" -c vllm -- \
                    curl -sf "http://localhost:${port}/metrics" 2>/dev/null \
                    | grep '^vllm:num_requests_running' | awk '{printf "%d", $2}') || N=0
                TOTAL=$((TOTAL + N))
            done
        done
        if [ "$TOTAL" -eq 0 ]; then
            echo "All requests drained."
            break
        fi
        echo "  $TOTAL requests still running, waiting 5s..."
        sleep 5
    done

# Clear NVMe KV cache on all prefill and decode nodes between benchmark runs.
clear-kv-cache:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    # Reset vLLM prefix cache (GPU + CPU tiers) via API
    for pod in $(kubectl get pods -n "$NS" -l llm-d.ai/role=prefill -o jsonpath='{.items[*].metadata.name}'); do
        echo "Resetting prefix cache on prefill $pod..."
        for port in 8000 8001 8002 8003 8004 8005 8006 8007; do
            kubectl exec -n "$NS" "$pod" -c vllm -- \
                curl -sf -X POST "http://localhost:${port}/reset_prefix_cache?reset_external=true" 2>/dev/null || true
        done
    done
    for pod in $(kubectl get pods -n "$NS" -l llm-d.ai/role=decode -o jsonpath='{.items[*].metadata.name}'); do
        echo "Resetting prefix cache on decode $pod..."
        for port in 8200 8201 8202 8203 8204 8205 8206 8207; do
            kubectl exec -n "$NS" "$pod" -c vllm -- \
                curl -sf -X POST "http://localhost:${port}/reset_prefix_cache?reset_external=true" 2>/dev/null || true
        done
    done
    # Clear NVMe filesystem tier
    for pod in $(kubectl get pods -n "$NS" -l llm-d.ai/role -o jsonpath='{.items[*].metadata.name}'); do
        echo "Clearing NVMe KV cache on $pod..."
        kubectl exec -n "$NS" "$pod" -c vllm -- rm -rf /mnt/nvme-cache/* 2>/dev/null || true
    done
    echo "All prefix caches reset (GPU + CPU + NVMe)."

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
# Usage: just scrape-grafana results_$USER-wide-ep-1p-ep8-1d-ep8/results_$USER-wide-ep-1p-ep8-1d-ep8_c1
scrape-grafana +dirs:
    python3 export_dashboard.py results {{dirs}}

# Scrape Grafana dashboards and generate interactivity chart.
# Reads namespace.txt from each result directory to find the right Grafana instance.
# Runs export_dashboard.py inside the aiperf pod via kubectl exec (no port-forward needed).
# Usage: just report results_routing
report outdir:
    #!/usr/bin/env bash
    set -euo pipefail
    DIRS=$(find "{{outdir}}" -name "profile_export_aiperf.json" -exec dirname {} \;)
    if [ -z "$DIRS" ]; then
        echo "No result directories found in {{outdir}}"
        exit 1
    fi
    SETUP_NAMESPACES=""
    for dir in $DIRS; do
        PARENT=$(dirname "$dir")
        if [ -f "$PARENT/namespace.txt" ]; then
            NS=$(cat "$PARENT/namespace.txt")
        else
            NS={{NAMESPACE}}
        fi
        # Copy script to pod once per namespace
        if ! echo "$SETUP_NAMESPACES" | grep -q "|${NS}|"; then
            POD=$(kubectl get pod -n "$NS" -l app={{deploy}} -o jsonpath='{.items[0].metadata.name}')
            kubectl cp export_dashboard.py "$NS/${POD}:/workspace/export_dashboard.py"
            SETUP_NAMESPACES="${SETUP_NAMESPACES}|${NS}|${POD}|"
        fi
        POD=$(echo "$SETUP_NAMESPACES" | grep -o "|${NS}|[^|]*|" | head -1 | cut -d'|' -f3)
        GRAFANA_URL="http://grafana.${NS}.svc.cluster.local:80"
        NAME=$(basename "$dir")
        echo "=== $NAME ($NS): scraping Grafana ==="
        TIMESTAMPS=$(python3 -c "import json; d=json.load(open('$dir/profile_export_aiperf.json')); print(d['min_request_timestamp']['avg']/1e9-60); print(d['max_response_timestamp']['avg']/1e9+60)")
        START=$(echo "$TIMESTAMPS" | head -1)
        END=$(echo "$TIMESTAMPS" | tail -1)
        echo "  Time range: $START → $END"
        kubectl exec -n "$NS" "$POD" -- python3 /workspace/export_dashboard.py \
            --grafana-url "$GRAFANA_URL" \
            single --start "$START" --end "$END" -o "/workspace/dashboard_${NAME}.html" || {
            echo "  WARNING: scrape failed for $NAME, skipping"
            continue
        }
        kubectl cp "$NS/${POD}:/workspace/dashboard_${NAME}.html" "$dir/dashboard.html" 2>/dev/null || true
        kubectl exec -n "$NS" "$POD" -- rm -f "/workspace/dashboard_${NAME}.html"
    done
    # Snapshot Prometheus TSDB for each namespace (preserves all metrics)
    SNAPPED=""
    for dir in $DIRS; do
        PARENT=$(dirname "$dir")
        if [ -f "$PARENT/namespace.txt" ]; then
            NS=$(cat "$PARENT/namespace.txt")
        else
            NS={{NAMESPACE}}
        fi
        if echo "$SNAPPED" | grep -q "|${NS}|"; then continue; fi
        SNAPPED="${SNAPPED}|${NS}|"
        PROM_POD=$(kubectl get pod -n "$NS" -l app.kubernetes.io/name=prometheus,app.kubernetes.io/component=server -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || continue
        echo "=== $NS: snapshotting Prometheus TSDB ==="
        SNAP_NAME=$(kubectl exec -n "$NS" "$PROM_POD" -c prometheus-server -- \
            wget -qO- --post-data= http://localhost:9090/api/v1/admin/tsdb/snapshot 2>/dev/null \
            | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['name'])" 2>/dev/null) || {
            echo "  WARNING: snapshot failed for $NS, skipping"
            continue
        }
        SNAP_DIR="$PARENT/prometheus_snapshot"
        mkdir -p "$SNAP_DIR"
        kubectl cp "$NS/${PROM_POD}:/data/snapshots/${SNAP_NAME}" "$SNAP_DIR" -c prometheus-server 2>/dev/null || {
            echo "  WARNING: snapshot copy failed for $NS"
            continue
        }
        kubectl exec -n "$NS" "$PROM_POD" -c prometheus-server -- rm -rf "/data/snapshots/${SNAP_NAME}" 2>/dev/null || true
        echo "  Saved to $SNAP_DIR"
    done
    python3 gen_interactivity_chart.py "$(dirname "{{outdir}}")" 2>/dev/null || true


# Configure monitoring, dashboards, and the benchmark runner in the existing namespace.
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    echo "=== Setting up benchmark support in namespace $NS ==="
    kubectl apply -n "$NS" -f - <<RBACEOF
    apiVersion: rbac.authorization.k8s.io/v1
    kind: Role
    metadata:
      name: prometheus-server
    rules:
    - apiGroups: [""]
      resources: [pods, services, endpoints]
      verbs: [get, list, watch]
    - apiGroups: [""]
      resources: [configmaps]
      verbs: [get]
    ---
    apiVersion: rbac.authorization.k8s.io/v1
    kind: RoleBinding
    metadata:
      name: prometheus-server
    roleRef:
      apiGroup: rbac.authorization.k8s.io
      kind: Role
      name: prometheus-server
    subjects:
    - kind: ServiceAccount
      name: prometheus-server
      namespace: ${NS}
    ---
    apiVersion: rbac.authorization.k8s.io/v1
    kind: Role
    metadata:
      name: grafana-sidecar
    rules:
    - apiGroups: [""]
      resources: [configmaps, secrets]
      verbs: [get, list, watch]
    ---
    apiVersion: rbac.authorization.k8s.io/v1
    kind: RoleBinding
    metadata:
      name: grafana-sidecar
    roleRef:
      apiGroup: rbac.authorization.k8s.io
      kind: Role
      name: grafana-sidecar
    subjects:
    - kind: ServiceAccount
      name: grafana
      namespace: ${NS}
    RBACEOF
    kubectl apply -f - <<DCGMRBAC || echo "WARNING: could not create cluster-scope dcgm reader RBAC; continuing"
    apiVersion: rbac.authorization.k8s.io/v1
    kind: ClusterRole
    metadata:
      name: ${NS}-prometheus-cw-exporters-reader
    rules:
    - apiGroups: [""]
      resources: [pods]
      verbs: [get, list, watch]
    ---
    apiVersion: rbac.authorization.k8s.io/v1
    kind: ClusterRoleBinding
    metadata:
      name: ${NS}-prometheus-cw-exporters-reader
    roleRef:
      apiGroup: rbac.authorization.k8s.io
      kind: ClusterRole
      name: ${NS}-prometheus-cw-exporters-reader
    subjects:
    - kind: ServiceAccount
      name: prometheus-server
      namespace: ${NS}
    DCGMRBAC
    if ! kubectl get secret hf-secret -n "$NS" >/dev/null 2>&1; then
        if kubectl get secret llm-d-hf-token -n "$NS" -o json 2>/dev/null \
          | python3 -c "import json,sys; d=json.load(sys.stdin); d['metadata']={'name':'hf-secret','namespace':'$NS'}; print(json.dumps(d))" \
          | kubectl apply -f - 2>/dev/null; then
            true
        else
            kubectl create secret generic hf-secret --from-literal=HF_TOKEN=dummy -n "$NS" --dry-run=client -o yaml | kubectl apply -f -
        fi
    fi
    helm upgrade --install prometheus prometheus-community/prometheus \
      -n "$NS" --version 29.13.0 \
      --set alertmanager.enabled=false \
      --set kube-state-metrics.enabled=false \
      --set prometheus-node-exporter.enabled=false \
      --set prometheus-pushgateway.enabled=false \
      --set rbac.create=false \
      --set server.persistentVolume.enabled=false \
      --set 'server.extraFlags[0]=web.enable-admin-api' \
      --set serviceAccounts.server.create=true \
      --set serviceAccounts.server.name=prometheus-server \
      --set 'server.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].key=kubernetes.io/arch' \
      --set 'server.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].operator=In' \
      --set 'server.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].values[0]=amd64' \
      -f - <<PROMEOF
    extraScrapeConfigs: |
      - job_name: 'vllm-decode'
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names: [$NS]
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_label_llm_d_ai_role]
            regex: decode
            action: keep
          - source_labels: [__meta_kubernetes_pod_container_name]
            regex: vllm
            action: keep
          - source_labels: [__meta_kubernetes_pod_container_port_number]
            regex: '8200'
            action: keep
          - source_labels: [__meta_kubernetes_pod_name]
            target_label: pod
          - source_labels: [__meta_kubernetes_pod_node_name]
            target_label: node
          - source_labels: [__meta_kubernetes_pod_label_llm_d_ai_model]
            target_label: model
        scrape_interval: 1s
        metrics_path: /metrics
      - job_name: 'vllm-prefill'
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names: [$NS]
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_label_llm_d_ai_role]
            regex: prefill
            action: keep
          - source_labels: [__meta_kubernetes_pod_container_name]
            regex: vllm
            action: keep
          - source_labels: [__meta_kubernetes_pod_container_port_number]
            regex: '8000'
            action: keep
          - source_labels: [__meta_kubernetes_pod_name]
            target_label: pod
          - source_labels: [__meta_kubernetes_pod_node_name]
            target_label: node
          - source_labels: [__meta_kubernetes_pod_label_llm_d_ai_model]
            target_label: model
        scrape_interval: 1s
        metrics_path: /metrics
      - job_name: 'epp'
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names: [$NS]
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_component]
            regex: epp
            action: keep
          - source_labels: [__meta_kubernetes_pod_ip]
            target_label: __address__
            replacement: '\${1}:9090'
          - source_labels: [__meta_kubernetes_pod_name]
            target_label: pod
          - source_labels: [__meta_kubernetes_pod_label_inferencepool]
            target_label: inferencepool
        scrape_interval: 1s
        metrics_path: /metrics
      - job_name: 'dcgm'
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names: [cw-exporters]
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_name]
            regex: dcgm-exporter
            action: keep
          - source_labels: [__meta_kubernetes_pod_node_name]
            target_label: node
        metric_relabel_configs:
          - source_labels: [Hostname]
            target_label: node
        scrape_interval: 5s
        metrics_path: /metrics
      - job_name: 'node-exporter'
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names: [$NS]
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_container_name]
            regex: node-exporter
            action: keep
          - source_labels: [__meta_kubernetes_pod_container_port_number]
            regex: '9100'
            action: keep
          - source_labels: [__meta_kubernetes_pod_name]
            target_label: pod
          - source_labels: [__meta_kubernetes_pod_node_name]
            target_label: node
          - source_labels: [__meta_kubernetes_pod_label_llm_d_ai_role]
            target_label: role
        scrape_interval: 5s
        metrics_path: /metrics
    PROMEOF
    helm upgrade --install grafana grafana/grafana \
      -n "$NS" --version 10.5.15 \
      --set adminPassword=admin \
      --set persistence.enabled=false \
      --set rbac.create=false \
      --set sidecar.dashboards.enabled=true \
      --set sidecar.dashboards.label=grafana_dashboard \
      --set sidecar.dashboards.searchNamespace="$NS" \
      --set 'affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].key=kubernetes.io/arch' \
      --set 'affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].operator=In' \
      --set 'affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].values[0]=amd64' \
      -f - <<GRAFEOF
    datasources:
      datasources.yaml:
        apiVersion: 1
        datasources:
        - name: Prometheus
          type: prometheus
          url: http://prometheus-server.${NS}.svc.cluster.local
          access: proxy
          isDefault: true
    GRAFEOF
    kubectl create configmap wideep-overview-dashboard \
        --from-file=wideep-overview.json=dashboards/grafana-wideep-overview.json \
        -n "$NS" --dry-run=client -o yaml \
      | kubectl label --local -f - grafana_dashboard=1 -o yaml \
      | kubectl apply -f -
    kubectl create configmap aggregate-overview-dashboard \
        --from-file=aggregate-overview.json=dashboards/grafana-aggregate.json \
        -n "$NS" --dry-run=client -o yaml \
      | kubectl label --local -f - grafana_dashboard=1 -o yaml \
      | kubectl apply -f -
    just deploy
    echo "=== Benchmark support ready in $NS ==="

_manifesto-info:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{manifesto_root}}"
    uv run python - <<'PY'
    from manifesto.cluster import load_cluster
    from manifesto.instance import Instance
    from manifesto.spec import load_spec
    spec = load_spec("{{manifesto_spec}}", load_cluster("{{manifesto_cluster}}"))
    instance = Instance("{{manifesto_user}}", spec.release).instance_id
    roles = {r.name: r for r in spec.roles}
    def role_gpus(name):
        r = roles.get(name)
        return 0 if r is None else r.lws.size * r.lws.replicas * r.gpus_per_pod
    print(f"instance={instance}")
    print(f"model={spec.model.id}")
    print(f"model_label={spec.model.label}")
    print(f"release={spec.release}")
    print(f"decode_gpus={role_gpus('decode')}")
    print(f"prefill_gpus={role_gpus('prefill')}")
    print(f"pods={spec.release}")
    PY

_model-url:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "{{url}}" ]; then
        printf '%s\n' "{{url}}"
        exit 0
    fi
    cd "{{manifesto_root}}"
    GATEWAY_SVC=$(uv run manifesto name "{{manifesto_spec}}" inference-gateway-istio --user "{{manifesto_user}}")
    printf 'http://%s:80/v1\n' "$GATEWAY_SVC"

start-model:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{manifesto_root}}"
    MANIFESTO_NAMESPACE="{{NAMESPACE}}" MANIFESTO_CLUSTER="{{manifesto_cluster}}" USER="{{manifesto_user}}" \
        just start "{{manifesto_spec}}" {{manifesto_args}}
    MANIFESTO_NAMESPACE="{{NAMESPACE}}" MANIFESTO_CLUSTER="{{manifesto_cluster}}" USER="{{manifesto_user}}" \
        just ready "{{manifesto_spec}}"
    just clear-kv-cache

stop-model:
    cd "{{manifesto_root}}" && MANIFESTO_NAMESPACE="{{NAMESPACE}}" MANIFESTO_CLUSTER="{{manifesto_cluster}}" USER="{{manifesto_user}}" just stop "{{manifesto_spec}}"

sweep-concurrency config_name dest="." duration="900":
    #!/usr/bin/env bash
    set -uo pipefail
    FAILED=""
    for C in 1 16 64 256; do
        RDIR="{{dest}}/results_{{config_name}}_c${C}"
        if [ -f "$RDIR/profile_export_aiperf.json" ]; then
            echo "=== concurrency=$C already exists, skipping ==="
            continue
        fi
        echo "=== concurrency=$C ({{duration}}s) ==="
        just drain
        just clear-kv-cache
        if ! just warmup || ! just run $C {{duration}}; then
            echo "FAILED: concurrency=$C, skipping"
            FAILED="${FAILED} c${C}"
            just wipe 2>/dev/null || true
            continue
        fi
        just results "$RDIR"
        just dump-logs "$RDIR"
        for attempt in 1 2 3 4 5; do
            if just report "{{dest}}"; then break; fi
            echo "Dashboard scrape attempt $attempt failed for c${C}, retrying in 10s..."
            sleep 10
        done
        just wipe
        sleep 10
    done
    if [ -n "$FAILED" ]; then
        echo "Failed concurrency levels:${FAILED}"
    fi

sweep outdir duration="900":
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "{{outdir}}"
    just wipe
    INFO=$(just --quiet _manifesto-info)
    CONFIG_NAME=$(printf '%s\n' "$INFO" | sed -n 's/^instance=//p')
    MODEL_ID=$(printf '%s\n' "$INFO" | sed -n 's/^model=//p')
    MODEL_LABEL=$(printf '%s\n' "$INFO" | sed -n 's/^model_label=//p')
    RELEASE=$(printf '%s\n' "$INFO" | sed -n 's/^release=//p')
    DECODE_GPUS=$(printf '%s\n' "$INFO" | sed -n 's/^decode_gpus=//p')
    PREFILL_GPUS=$(printf '%s\n' "$INFO" | sed -n 's/^prefill_gpus=//p')
    PODS=$(printf '%s\n' "$INFO" | sed -n 's/^pods=//p')
    dir="{{outdir}}/results_${CONFIG_NAME}"
    ALL_DONE=true
    for C in 1 16 64 256; do
        if [ ! -f "$dir/results_${CONFIG_NAME}_c${C}/profile_export_aiperf.json" ]; then
            ALL_DONE=false
            break
        fi
    done
    if [ "$ALL_DONE" = true ]; then
        echo "====== ${CONFIG_NAME} all concurrency levels done, skipping ======"
        exit 0
    fi
    echo "====== ${CONFIG_NAME} (${RELEASE}) ======"
    just stop-model 2>/dev/null || true
    just start-model
    just check
    just warmup
    mkdir -p "$dir"
    echo "{{NAMESPACE}}" > "$dir/namespace.txt"
    echo "$MODEL_ID" > "$dir/model.txt"
    echo "$MODEL_LABEL" > "$dir/model_label.txt"
    echo "$CONFIG_NAME" > "$dir/config_name.txt"
    echo "$RELEASE" > "$dir/config_label.txt"
    echo "$DECODE_GPUS" > "$dir/decode_gpus.txt"
    echo "$PREFILL_GPUS" > "$dir/prefill_gpus.txt"
    echo "$PODS" > "$dir/pods.txt"
    echo "{{manifesto_spec}}" > "$dir/manifesto_spec.txt"
    (cd "{{manifesto_root}}" && uv run manifesto instance-id "{{manifesto_spec}}" --user "{{manifesto_user}}") > "$dir/manifesto_instance.txt"
    INSTANCE=$(cat "$dir/manifesto_instance.txt")
    kubectl get all -n "{{NAMESPACE}}" -l app.kubernetes.io/instance="$INSTANCE" -o yaml > "$dir/manifesto-resources.yaml" 2>/dev/null || true
    kubectl get pod -n "{{NAMESPACE}}" -l llm-d.ai/role=prefill -o yaml > "$dir/prefill.yaml"
    kubectl get pod -n "{{NAMESPACE}}" -l llm-d.ai/role=decode -o yaml > "$dir/decode.yaml"
    kubectl get pod -n "{{NAMESPACE}}" -l app.kubernetes.io/component=epp -o yaml > "$dir/epp.yaml" 2>/dev/null || true
    kubectl get inferencepool -n "{{NAMESPACE}}" -o yaml > "$dir/inferencepool.yaml" 2>/dev/null || true
    kubectl get httproute -n "{{NAMESPACE}}" -o yaml > "$dir/httproute.yaml" 2>/dev/null || true
    just vllm-version "$dir"
    just sweep-concurrency "$CONFIG_NAME" "$dir" {{duration}}
    just stop-model

snapshot-prometheus ns dest:
    #!/usr/bin/env bash
    set -euo pipefail
    PROM_POD=$(kubectl get pod -n "{{ns}}" -l app.kubernetes.io/name=prometheus,app.kubernetes.io/component=server -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || {
        echo "  No Prometheus pod in {{ns}}, skipping snapshot"
        exit 0
    }
    echo "=== {{ns}}: snapshotting Prometheus TSDB ==="
    SNAP_NAME=$(kubectl exec -n "{{ns}}" "$PROM_POD" -c prometheus-server -- \
        wget -qO- --post-data= http://localhost:9090/api/v1/admin/tsdb/snapshot 2>/dev/null \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['name'])" 2>/dev/null) || {
        echo "  WARNING: snapshot failed for {{ns}}, skipping"
        exit 0
    }
    SNAP_DIR="{{dest}}/prometheus_snapshot"
    mkdir -p "$SNAP_DIR"
    kubectl cp "{{ns}}/${PROM_POD}:/data/snapshots/${SNAP_NAME}" "$SNAP_DIR" -c prometheus-server 2>/dev/null || {
        echo "  WARNING: snapshot copy failed for {{ns}}"
        exit 0
    }
    kubectl exec -n "{{ns}}" "$PROM_POD" -c prometheus-server -- rm -rf "/data/snapshots/${SNAP_NAME}" 2>/dev/null || true
    echo "  Saved to $SNAP_DIR"

seed_image := "quay.io/rh-ee-ecrncevi/benchmark-seed:amd64"
seed_deploy := "benchmark-seed"

seed-build:
    podman build --platform linux/amd64 -f Dockerfile.seed -t {{seed_image}} .
    podman push {{seed_image}}

seed-deploy:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl apply -n {{NAMESPACE}} -f seed.yaml
    kubectl rollout status deploy/{{seed_deploy}} -n {{NAMESPACE}} --timeout=300s
    POD=$(kubectl get pod -n {{NAMESPACE}} -l app={{seed_deploy}} -o jsonpath='{.items[0].metadata.name}')
    kubectl exec -n {{NAMESPACE}} "$POD" -- helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
    kubectl exec -n {{NAMESPACE}} "$POD" -- helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null || true
    kubectl exec -n {{NAMESPACE}} "$POD" -- helm repo update
    echo "Seed pod ready: $POD"

_seed-sync outdir duration:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    POD=$(kubectl get pod -n "$NS" -l app={{seed_deploy}} -o jsonpath='{.items[0].metadata.name}')
    kubectl exec -n "$NS" "$POD" -- mkdir -p /workspace/agentx-mvp /workspace/agentx-mvp/dashboards
    for f in Justfile agentx.yaml dashboard.json extract_timestamps.py export_dashboard.py gen_interactivity_chart.py overlay_dashboards.py; do
        [ -f "$f" ] && kubectl cp "$f" "$NS/${POD}:/workspace/agentx-mvp/$f"
    done
    for f in dashboards/*.json; do
        kubectl cp "$f" "$NS/${POD}:/workspace/agentx-mvp/$f"
    done
    kubectl exec -n "$NS" "$POD" -- rm -rf /workspace/llm-manifesto
    kubectl exec -n "$NS" "$POD" -- mkdir -p /workspace/llm-manifesto
    tar --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache -C "{{manifesto_root}}" -cf - . \
      | kubectl exec -i -n "$NS" "$POD" -- tar -xf - -C /workspace/llm-manifesto
    TMPENV=$(mktemp)
    trap "rm -f $TMPENV" EXIT
    printf 'NAMESPACE=%s\nMODEL_SPEC=%s\nMANIFESTO_ROOT=/workspace/llm-manifesto\nMANIFESTO_CLUSTER=%s\nMANIFESTO_USER=%s\nMANIFESTO_ARGS=%s\nMAX_CONTEXT_LENGTH=%s\n' \
      "{{NAMESPACE}}" "{{manifesto_spec}}" "{{manifesto_cluster}}" "{{manifesto_user}}" "{{manifesto_args}}" "{{max_context_length}}" > "$TMPENV"
    kubectl cp "$TMPENV" "$NS/${POD}:/workspace/agentx-mvp/.env"

seed outdir duration="900":
    #!/usr/bin/env bash
    set -euo pipefail
    just _seed-sync "{{outdir}}" "{{duration}}"
    NS={{NAMESPACE}}
    POD=$(kubectl get pod -n "$NS" -l app={{seed_deploy}} -o jsonpath='{.items[0].metadata.name}')
    TMPSCRIPT=$(mktemp)
    printf '#!/bin/bash\nset -euo pipefail\ncd /workspace/agentx-mvp\njust setup\njust sweep %s %s > /workspace/seed-sweep.log 2>&1\necho $? > /workspace/seed-sweep.exit_code\nrm -f /workspace/seed-sweep.pid\n' "{{outdir}}" "{{duration}}" > "$TMPSCRIPT"
    kubectl cp "$TMPSCRIPT" "$NS/${POD}:/workspace/run_seed.sh"
    rm -f "$TMPSCRIPT"
    kubectl exec -n "$NS" "$POD" -- chmod +x /workspace/run_seed.sh
    kubectl exec -n "$NS" "$POD" -- bash -c 'nohup bash /workspace/run_seed.sh </dev/null >/dev/null 2>&1 & echo $! > /workspace/seed-sweep.pid && echo "Launched PID $!"'
    echo "Sweep running detached. Monitor: just seed-logs"

seed-logs:
    POD=$(kubectl get pod -n {{NAMESPACE}} -l app={{seed_deploy}} -o jsonpath='{.items[0].metadata.name}')
    kubectl exec -n {{NAMESPACE}} "$POD" -- tail -f /workspace/seed-sweep.log

seed-results outdir:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    POD=$(kubectl get pod -n "$NS" -l app={{seed_deploy}} -o jsonpath='{.items[0].metadata.name}')
    DIRS=$(kubectl exec -n "$NS" "$POD" -- find /workspace/agentx-mvp/{{outdir}} -name "profile_export_aiperf.json" -exec dirname {} \; 2>/dev/null)
    for dir in $DIRS; do
        LOCAL=${dir#/workspace/agentx-mvp/}
        mkdir -p "$LOCAL"
        kubectl cp "$NS/${POD}:${dir}" "$LOCAL" 2>/dev/null || true
    done
    EXTRAS=$(kubectl exec -n "$NS" "$POD" -- find /workspace/agentx-mvp/{{outdir}} -maxdepth 2 \( -name "*.yaml" -o -name "*.txt" -o -name "*.html" \) 2>/dev/null)
    for f in $EXTRAS; do
        LOCAL=${f#/workspace/agentx-mvp/}
        mkdir -p "$(dirname "$LOCAL")"
        kubectl cp "$NS/${POD}:${f}" "$LOCAL" 2>/dev/null || true
    done
    echo "Results copied to {{outdir}}/"
    python3 gen_interactivity_chart.py "{{outdir}}" 2>/dev/null || true

seed-stop:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    POD=$(kubectl get pod -n "$NS" -l app={{seed_deploy}} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
    if [ -n "$POD" ]; then
        kubectl exec -n "$NS" "$POD" -- bash -c 'kill $(cat /workspace/seed-sweep.pid 2>/dev/null) 2>/dev/null; rm -f /workspace/seed-sweep.pid' 2>/dev/null || true
    fi
    just stop-model 2>/dev/null || true
    echo "Sweep stopped; namespace preserved."

seed-clean:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl delete deploy {{seed_deploy}} -n {{NAMESPACE}} --ignore-not-found
    kubectl delete rolebinding benchmark-orchestrator -n {{NAMESPACE}} --ignore-not-found
    kubectl delete role benchmark-orchestrator -n {{NAMESPACE}} --ignore-not-found
    kubectl delete clusterrolebinding benchmark-orchestrator --ignore-not-found 2>/dev/null || true
    kubectl delete sa benchmark-orchestrator -n {{NAMESPACE}} --ignore-not-found
    echo "Seed pod cleaned up."
