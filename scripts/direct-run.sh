#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env
require_agentx_env KUBE_CONTEXT NAMESPACE URL MODEL AIPERF_IMAGE

CONCURRENCY="${CONCURRENCY:-1}"
DURATION_SECONDS="${DURATION_SECONDS:-1800}"
CONTEXT_PROFILE="${CONTEXT_PROFILE:-limited}"
case "$CONTEXT_PROFILE" in
  limited) AIPERF_MAX_CONTEXT_LENGTH="${AIPERF_MAX_CONTEXT_LENGTH:-${MAX_CONTEXT_LENGTH:-142000}}" ;;
  full) AIPERF_MAX_CONTEXT_LENGTH="${AIPERF_MAX_CONTEXT_LENGTH:-1048576}" ;;
  *) echo "ERROR: CONTEXT_PROFILE must be limited or full" >&2; exit 2 ;;
esac
AGENTX_DATASET="${AGENTX_DATASET:-semianalysis_cc_traces_weka_062126}"
TOKENIZER="${TOKENIZER:-zai-org/GLM-5.2-FP8}"
MODEL_REVISION="${MODEL_REVISION:-ba978f7d347eaf65d22f1a86833408afdb953541}"
RANDOM_SEED="${RANDOM_SEED:-20260827}"
RESET_PREFIX_CACHES="${RESET_PREFIX_CACHES:-true}"
RESULTS_MOUNT="${RESULTS_MOUNT:-/results}"
RESULTS_MODE="${AIPERF_RESULTS_MODE:-pvc}"
LOCAL_RESULTS_ROOT="${LOCAL_RESULTS_ROOT:-results/.artifacts}"
ARTIFACT_SUBDIR="${ARTIFACT_SUBDIR:-}"
CPU_REQUEST="${CPU_REQUEST:-4}"
CPU_LIMIT="${CPU_LIMIT:-16}"
MEMORY_REQUEST="${MEMORY_REQUEST:-32Gi}"
MEMORY_LIMIT="${MEMORY_LIMIT:-64Gi}"
AIPERF_ARCH="${AIPERF_ARCH:-amd64}"
HF_TOKEN_SECRET="${HF_TOKEN_SECRET:-}"
KUEUE_QUEUE="${KUEUE_QUEUE:-}"   # when set, the Job is created suspended and Kueue admits it

for value in "$CONCURRENCY" "$DURATION_SECONDS" "$AIPERF_MAX_CONTEXT_LENGTH" "$RANDOM_SEED"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
    echo "ERROR: concurrency, duration, and context length must be positive integers" >&2
    exit 2
  }
done
((DURATION_SECONDS >= 60)) || { echo "ERROR: DURATION_SECONDS must be at least 60" >&2; exit 2; }
[[ "$KUBE_CONTEXT" =~ ^[A-Za-z0-9._:@/-]+$ ]] || { echo "ERROR: invalid KUBE_CONTEXT" >&2; exit 2; }
[[ "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] || { echo "ERROR: namespace must be a Kubernetes DNS name" >&2; exit 2; }
case "$RESULTS_MODE" in
  pvc)
    require_agentx_env RESULTS_PVC
    [[ "$RESULTS_PVC" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] || { echo "ERROR: RESULTS_PVC must be a Kubernetes DNS name" >&2; exit 2; }
    ;;
  pod) RESULTS_MOUNT=/tmp/aiperf-results ;;
  *) echo "ERROR: AIPERF_RESULTS_MODE must be pvc or pod" >&2; exit 2 ;;
esac
[[ "$URL" =~ ^https?://[A-Za-z0-9._:/-]+$ ]] || { echo "ERROR: invalid URL" >&2; exit 2; }
[[ "$MODEL" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "ERROR: invalid MODEL" >&2; exit 2; }
[[ "$TOKENIZER" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "ERROR: invalid TOKENIZER" >&2; exit 2; }
[[ "$MODEL_REVISION" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "ERROR: invalid MODEL_REVISION" >&2; exit 2; }
[[ "$AGENTX_DATASET" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "ERROR: invalid AGENTX_DATASET" >&2; exit 2; }
[[ "$AIPERF_IMAGE" =~ ^[A-Za-z0-9._:/@-]+$ ]] || { echo "ERROR: invalid AIPERF_IMAGE" >&2; exit 2; }
[[ "$AIPERF_ARCH" == amd64 || "$AIPERF_ARCH" == arm64 ]] || { echo "ERROR: AIPERF_ARCH must be amd64 or arm64" >&2; exit 2; }
[[ "$RESET_PREFIX_CACHES" == true || "$RESET_PREFIX_CACHES" == false ]] || { echo "ERROR: RESET_PREFIX_CACHES must be true or false" >&2; exit 2; }
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
pod=""
cleanup() {
  rm -f "$manifest"
  if [[ "$RESULTS_MODE" == pod && -n "$pod" ]]; then
    k exec "$pod" -- touch "${artifact_dir}/.copy-complete" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if ((DURATION_SECONDS < 900)); then
  unsafe_override=true
  require_submission_valid=false
else
  unsafe_override=false
  require_submission_valid=true
fi

secret_yaml=""
if [[ -n "$HF_TOKEN_SECRET" ]]; then
  secret_yaml=$(cat <<EOF
          envFrom:
            - secretRef:
                name: ${HF_TOKEN_SECRET}
EOF
)
fi

kueue_label=""; suspend=false
if [[ -n "$KUEUE_QUEUE" ]]; then
  kueue_label="    kueue.x-k8s.io/queue-name: ${KUEUE_QUEUE}"; suspend=true   # Kueue admits (unsuspends) the Job
fi
results_mount_yaml=""
results_volume_yaml=""
if [[ "$RESULTS_MODE" == pvc ]]; then
  results_mount_yaml=$(cat <<EOF
          volumeMounts:
            - name: results
              mountPath: ${RESULTS_MOUNT}
EOF
)
  results_volume_yaml=$(cat <<EOF
      volumes:
        - name: results
          persistentVolumeClaim:
            claimName: ${RESULTS_PVC}
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
${kueue_label}
spec:
  suspend: ${suspend}
  backoffLimit: 0
  activeDeadlineSeconds: $((DURATION_SECONDS + 7200))
  template:
    metadata:
      labels:
        app.kubernetes.io/name: agentx-aiperf
    spec:
      nodeSelector:
        kubernetes.io/arch: ${AIPERF_ARCH}
      restartPolicy: Never
      containers:
        - name: aiperf
          image: ${AIPERF_IMAGE}
          imagePullPolicy: IfNotPresent
${secret_yaml}
          env:
            - name: HF_HOME
              value: /workspace/.cache/huggingface
            - name: AIPERF_UNSAFE_OVERRIDE
              value: "${unsafe_override}"
            - name: REQUIRE_SUBMISSION_VALID
              value: "${require_submission_valid}"
          command: ["/bin/bash", "-lc"]
          args:
            - |-
              set -euo pipefail
              artifact_dir='${artifact_dir}'
              mkdir -p "\$artifact_dir/logs"
              cat >"\$artifact_dir/agentx-config.env" <<'CONFIG'
              SCENARIO=inferencex-agentx-mvp
              DATASET=${AGENTX_DATASET}
              MODEL=${MODEL}
              TOKENIZER=${TOKENIZER}
              TOKENIZER_REVISION=${MODEL_REVISION}
              CONTEXT_PROFILE=${CONTEXT_PROFILE}
              MAX_CONTEXT_LENGTH=${AIPERF_MAX_CONTEXT_LENGTH}
              CONCURRENCY=${CONCURRENCY}
              DURATION_SECONDS=${DURATION_SECONDS}
              RANDOM_SEED=${RANDOM_SEED}
              RESET_PREFIX_CACHES=${RESET_PREFIX_CACHES}
              CONFIG
              served_max_context="\$(/opt/venv/bin/python -c 'import json, sys, urllib.request; payload=json.load(urllib.request.urlopen(sys.argv[1].rstrip("/")+"/v1/models", timeout=30)); print(next(item["max_model_len"] for item in payload["data"] if item["id"] == sys.argv[2]))' '${URL}' '${MODEL}')"
              if ((served_max_context < ${AIPERF_MAX_CONTEXT_LENGTH})); then
                echo "ERROR: AgentX cap ${AIPERF_MAX_CONTEXT_LENGTH} exceeds served max_model_len \$served_max_context" >&2
                exit 2
              fi
              scenario_args=()
              if [[ "\$AIPERF_UNSAFE_OVERRIDE" == true ]]; then
                scenario_args+=(--unsafe-override)
              fi
              if [[ '${RESET_PREFIX_CACHES}' == true ]]; then
                /opt/venv/bin/python - '${URL}' <<'PY'
              import json
              import sys
              import time
              import urllib.request

              endpoint = sys.argv[1].rstrip("/") + "/reset_prefix_cache?reset_external=true"
              for attempt in range(60):
                  try:
                      request = urllib.request.Request(endpoint, method="POST")
                      with urllib.request.urlopen(request, timeout=10) as response:
                          if json.load(response).get("success") is True:
                              print("Prefix caches reset")
                              break
                  except Exception as error:
                      if attempt == 59:
                          raise SystemExit(f"Could not reset prefix caches: {error}")
                  time.sleep(2)
              else:
                  raise SystemExit("Could not reset prefix caches")
              PY
              fi
              set +e
              /opt/venv/bin/aiperf profile \
                --scenario inferencex-agentx-mvp \
                "\${scenario_args[@]}" \
                --url '${URL}' \
                --model '${MODEL}' \
                --tokenizer '${TOKENIZER}' \
                --tokenizer-revision '${MODEL_REVISION}' \
                --max-context-length '${AIPERF_MAX_CONTEXT_LENGTH}' \
                --endpoint-type chat \
                --use-server-token-count \
                --public-dataset '${AGENTX_DATASET}' \
                --concurrency '${CONCURRENCY}' \
                --benchmark-duration '${DURATION_SECONDS}' \
                --random-seed '${RANDOM_SEED}' \
                --artifact-dir "\$artifact_dir" \
                --ui none \
                2>&1 | tee "\$artifact_dir/logs/aiperf.log"
              run_rc=\${PIPESTATUS[0]}
              summary="\$artifact_dir/profile_export_aiperf.json"
              if ((run_rc == 0)); then
                test -f "\$summary"
                run_rc=\$?
              fi
              if ((run_rc == 0)); then
                /opt/venv/bin/python -c 'import json, sys; data=json.load(open(sys.argv[1])); assert data["output_token_throughput"]["avg"] > 0; assert data["output_token_throughput_per_user"]["avg"] > 0; assert sys.argv[2] != "true" or data.get("metadata", {}).get("submission_valid") is True' "\$summary" "\$REQUIRE_SUBMISSION_VALID"
                run_rc=\$?
              fi
              set -e
              echo "\$run_rc" >"\$artifact_dir/.runner-exit-code"
              if [[ '${RESULTS_MODE}' == pod ]]; then
                echo "AIPerf finished (rc=\$run_rc); waiting for artifact copy acknowledgement"
                while [[ ! -f "\$artifact_dir/.copy-complete" ]]; do sleep 2; done
              fi
              exit "\$run_rc"
          resources:
            requests:
              cpu: '${CPU_REQUEST}'
              memory: '${MEMORY_REQUEST}'
            limits:
              cpu: '${CPU_LIMIT}'
              memory: '${MEMORY_LIMIT}'
${results_mount_yaml}
${results_volume_yaml}
EOF

echo "Submitting ${job} to ${KUBE_CONTEXT}/${NAMESPACE}"
if [[ "$RESULTS_MODE" == pvc ]]; then
  echo "Artifacts: ${RESULTS_PVC}:${artifact_dir}"
else
  echo "Artifacts: pod:${artifact_dir} -> ${LOCAL_RESULTS_ROOT}/${ARTIFACT_SUBDIR:-${timestamp}_c${CONCURRENCY}_${DURATION_SECONDS}s}"
fi
k apply -f "$manifest"
# Poll instead of `kubectl wait --for=condition=complete`, which never returns for a Failed Job.
deadline=$(( $(date +%s) + DURATION_SECONDS + 1800 ))
if [[ "$RESULTS_MODE" == pod ]]; then
  while [[ -z "$pod" ]]; do
    pod="$(k get pods -l "job-name=${job}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    (( $(date +%s) < deadline )) || { echo "ERROR: job $job did not create a pod in time" >&2; exit 1; }
    [[ -n "$pod" ]] || sleep 2
  done
  while ! k exec "$pod" -- test -f "${artifact_dir}/.runner-exit-code" >/dev/null 2>&1; do
    phase="$(k get "pod/$pod" -o jsonpath='{.status.phase}')"
    if [[ "$phase" == Failed || "$phase" == Succeeded ]]; then
      echo "ERROR: pod $pod exited before artifact handoff" >&2
      k logs "$pod" --tail=60 >&2 || true
      exit 1
    fi
    (( $(date +%s) < deadline )) || { echo "ERROR: job $job did not finish AIPerf in time" >&2; k describe "job/$job" >&2 || true; exit 1; }
    sleep 10
  done
  run_rc="$(k exec "$pod" -- cat "${artifact_dir}/.runner-exit-code")"
  [[ "$run_rc" =~ ^[0-9]+$ ]] || { echo "ERROR: invalid runner exit code: $run_rc" >&2; exit 1; }
  local_dir="${LOCAL_RESULTS_ROOT}/${ARTIFACT_SUBDIR:-${timestamp}_c${CONCURRENCY}_${DURATION_SECONDS}s}"
  if [[ -e "$local_dir" ]]; then
    echo "ERROR: refusing to overwrite local artifact path: $local_dir" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$local_dir")"
  k cp "${pod}:${artifact_dir}" "$local_dir"
  k exec "$pod" -- touch "${artifact_dir}/.copy-complete"
  echo "Copied artifacts to $local_dir"
fi
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
if [[ "$RESULTS_MODE" == pod ]]; then
  if ((run_rc != 0)); then
    echo "ERROR: AIPerf runner failed with exit code $run_rc (artifacts copied)" >&2
    exit "$run_rc"
  fi
fi
