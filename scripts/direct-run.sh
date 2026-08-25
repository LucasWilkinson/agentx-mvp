#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env
require_agentx_env KUBE_CONTEXT NAMESPACE URL MODEL RESULTS_PVC AIPERF_IMAGE

CONCURRENCY="${CONCURRENCY:-16}"
DURATION_SECONDS="${DURATION_SECONDS:-300}"
MAX_CONTEXT_LENGTH="${MAX_CONTEXT_LENGTH:-120000}"
RANDOM_SEED="${RANDOM_SEED:-42}"
RESULTS_MOUNT="${RESULTS_MOUNT:-/results}"
ARTIFACT_SUBDIR="${ARTIFACT_SUBDIR:-}"
CPU_REQUEST="${CPU_REQUEST:-4}"
CPU_LIMIT="${CPU_LIMIT:-16}"
MEMORY_REQUEST="${MEMORY_REQUEST:-32Gi}"
MEMORY_LIMIT="${MEMORY_LIMIT:-64Gi}"
HF_TOKEN_SECRET="${HF_TOKEN_SECRET:-}"

for value in "$CONCURRENCY" "$DURATION_SECONDS" "$MAX_CONTEXT_LENGTH" "$RANDOM_SEED"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
    echo "ERROR: concurrency, duration, and context length must be positive integers" >&2
    exit 2
  }
done
[[ "$KUBE_CONTEXT" =~ ^[A-Za-z0-9._:@/-]+$ ]] || { echo "ERROR: invalid KUBE_CONTEXT" >&2; exit 2; }
for value in "$NAMESPACE" "$RESULTS_PVC"; do
  [[ "$value" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] || {
    echo "ERROR: namespace and PVC must be Kubernetes DNS names" >&2
    exit 2
  }
done
[[ "$URL" =~ ^https?://[A-Za-z0-9._:/-]+$ ]] || { echo "ERROR: invalid URL" >&2; exit 2; }
[[ "$MODEL" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "ERROR: invalid MODEL" >&2; exit 2; }
[[ "$AIPERF_IMAGE" =~ ^[A-Za-z0-9._:/@-]+$ ]] || { echo "ERROR: invalid AIPERF_IMAGE" >&2; exit 2; }
for value in "$RESULTS_PREFIX" "${ARTIFACT_SUBDIR:-x}"; do
  [[ "$value" =~ ^[A-Za-z0-9._/-]+$ && "$value" != /* && "$value" != *..* ]] || {
    echo "ERROR: RESULTS_PREFIX / ARTIFACT_SUBDIR must be safe relative paths" >&2
    exit 2
  }
done
if [[ -n "$HF_TOKEN_SECRET" && ! "$HF_TOKEN_SECRET" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
  echo "ERROR: invalid HF_TOKEN_SECRET" >&2
  exit 2
fi
[[ "$RESULTS_MOUNT" == /* && "$RESULTS_MOUNT" != / ]] || {
  echo "ERROR: RESULTS_MOUNT must be an absolute, non-root path" >&2
  exit 2
}

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
timestamp_slug="$(printf '%s' "$timestamp" | tr '[:upper:]' '[:lower:]')"
job="agentx-c${CONCURRENCY}-${timestamp_slug}"
artifact_dir="${RESULTS_MOUNT}/${RESULTS_PREFIX}/${ARTIFACT_SUBDIR:-${timestamp}_c${CONCURRENCY}_${DURATION_SECONDS}s}"
manifest="$(mktemp)"
trap 'rm -f "$manifest"' EXIT

secret_yaml=""
if [[ -n "$HF_TOKEN_SECRET" ]]; then
  secret_yaml=$(cat <<EOF
          envFrom:
            - secretRef:
                name: ${HF_TOKEN_SECRET}
EOF
)
fi

cat >"$manifest" <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: agentx-aiperf
spec:
  backoffLimit: 0
  activeDeadlineSeconds: $((DURATION_SECONDS + 7200))
  template:
    metadata:
      labels:
        app.kubernetes.io/name: agentx-aiperf
    spec:
      restartPolicy: Never
      containers:
        - name: aiperf
          image: ${AIPERF_IMAGE}
          imagePullPolicy: IfNotPresent
${secret_yaml}
          env:
            - name: AIPERF_DATASET_WEKA_LIVE_ASSISTANT_RESPONSES
              value: "1"
            - name: HF_HOME
              value: /workspace/.cache/huggingface
          command: ["/bin/bash", "-lc"]
          args:
            - |-
              set -euo pipefail
              artifact_dir='${artifact_dir}'
              mkdir -p "\$artifact_dir/logs"
              /opt/venv/bin/aiperf profile \
                --scenario inferencex-agentx-mvp \
                --unsafe-override \
                --url '${URL}' \
                --model '${MODEL}' \
                --max-context-length '${MAX_CONTEXT_LENGTH}' \
                --endpoint-type chat \
                --streaming \
                --use-server-token-count \
                --tokenizer-trust-remote-code \
                --public-dataset semianalysis_cc_traces_weka_with_subagents \
                --concurrency '${CONCURRENCY}' \
                --benchmark-duration '${DURATION_SECONDS}' \
                --random-seed '${RANDOM_SEED}' \
                --no-server-metrics \
                --no-gpu-telemetry \
                --output-artifact-dir "\$artifact_dir" \
                --ui simple \
                2>&1 | tee "\$artifact_dir/logs/aiperf.log"
              test -f "\$artifact_dir/profile_export_aiperf.json"
          resources:
            requests:
              cpu: '${CPU_REQUEST}'
              memory: '${MEMORY_REQUEST}'
            limits:
              cpu: '${CPU_LIMIT}'
              memory: '${MEMORY_LIMIT}'
          volumeMounts:
            - name: results
              mountPath: ${RESULTS_MOUNT}
      volumes:
        - name: results
          persistentVolumeClaim:
            claimName: ${RESULTS_PVC}
EOF

echo "Submitting ${job} to ${KUBE_CONTEXT}/${NAMESPACE}"
echo "Artifacts: ${RESULTS_PVC}:${artifact_dir}"
k apply -f "$manifest"
# Poll instead of `kubectl wait --for=condition=complete`, which never returns for a Failed Job.
deadline=$(( $(date +%s) + DURATION_SECONDS + 1800 ))
while :; do
  status="$(k get "job/$job" -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type}{end}')"
  case "$status" in
    *Complete*) break ;;
    *Failed*) echo "ERROR: job $job failed" >&2; k describe "job/$job" >&2 || true; k logs "job/$job" --tail=60 >&2 || true; exit 1 ;;
  esac
  (( $(date +%s) < deadline )) || { echo "ERROR: job $job did not finish in time" >&2; k describe "job/$job" >&2 || true; exit 1; }
  sleep 15
done
k logs "job/$job"
