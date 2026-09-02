#!/usr/bin/env bash
set -u

cd "$(dirname "$0")/.."

readonly context=default
readonly namespace=vllm
readonly devbox=lwilkinson-vllm-devbox
readonly interval="${MONITOR_INTERVAL_SECONDS:-1200}"
readonly local_root=results/.artifacts/gb200-frankenstein-token-a2a-994b
readonly remote_root=/mnt/lustre/lwilkinson/benchmarks/lmsys-glm-agentic/results/frankenstein-token-a2a-994b
readonly evalscope=/mnt/lustre/lwilkinson/benchmarks/lmsys-glm-agentic/evalscope-venv-acd09b44384d53174768bb1063f675420f76fae9/bin/evalscope
readonly dataset=/mnt/lustre/lwilkinson/benchmarks/lmsys-glm-agentic/openhand-zai-org-GLM-5.3-48534d2fdaafc810.json

mkdir -p "$local_root"

k() {
  kubectl --context "$context" -n "$namespace" --request-timeout=90s "$@"
}

remote() {
  k exec "$devbox" -- bash -lc "$1"
}

summary_count() {
  local arm=$1
  remote "find '$remote_root/$arm' -name benchmark_summary.json -type f 2>/dev/null | wc -l" 2>/dev/null | tr -d '[:space:]'
}

clean_summary_count() {
  local arm=$1
  remote "find '$remote_root/$arm' -name benchmark_summary.json -type f -print0 2>/dev/null | xargs -0 -r jq -r 'select(.\"Failed Requests\" == 0) | 1' | wc -l" 2>/dev/null | tr -d '[:space:]'
}

client_state() {
  local arm=$1
  remote "root='$remote_root/$arm'; if [[ -f \"\$root/client.pid\" ]]; then p=\$(cat \"\$root/client.pid\"); ps -p \"\$p\" -o pid=,etime=,stat=,cmd= || true; else echo not-launched; fi" 2>/dev/null
}

release_completed_arm() {
  local arm=$1
  shift
  local count clean
  count=$(summary_count "$arm")
  clean=$(clean_summary_count "$arm")
  [[ "$count" =~ ^[0-9]+$ ]] || return 0
  [[ "$clean" =~ ^[0-9]+$ ]] || return 0
  # Four files alone are not completion: EvalScope writes summaries even when
  # requests fail. Never release a deployment for an invalid Pareto.
  (( count >= 4 && clean >= 4 )) || return 0
  local lws
  for lws in "$@"; do
    if k get lws "$lws" >/dev/null 2>&1; then
      echo "Sweep $arm has all four summaries; releasing $lws."
      k delete lws "$lws" --wait=false || true
    fi
  done
}

tp_ready() {
  local pods
  pods=$(k get pods -o json 2>/dev/null) || return 1
  jq -e '
    [.items[]
      | select(.metadata.name | startswith("lwilkinson-glm53-gb200-tp8-dp8-"))
      | select(.metadata.deletionTimestamp == null)] as $pods
    | ($pods | length) == 4
      and all($pods[]; any(.status.conditions[]?; .type == "Ready" and .status == "True"))
  ' <<<"$pods" >/dev/null
}

start_tp_client() {
  local existing count
  count=$(summary_count tp8-ep8)
  [[ "$count" =~ ^[0-9]+$ ]] || count=0
  (( count < 4 )) || return 0
  existing=$(client_state tp8-ep8)
  if [[ "$existing" != not-launched && "$existing" != *" Z "* && "$existing" != *" Z+ "* && -n "$existing" ]]; then
    return 0
  fi
  tp_ready || return 0

  echo "TP8 server is Ready; launching its c1/c2/c4/c8 client."
  remote "
    root='$remote_root/tp8-ep8'
    mkdir -p \"\$root\"
    nohup '$evalscope' perf \\
      --model glm-agentx-tep-graph \\
      --url http://lwilkinson-tep8-router-epp:80/v1/chat/completions \\
      --api openai --dataset swe_smith --dataset-path '$dataset' \\
      --max-tokens 220 --multi-turn --number 4 8 8 16 --parallel 1 2 4 8 \\
      --extra-args '{\"ignore_eos\":true}' --name tp8-ep8-994b \\
      --outputs-dir \"\$root\" --no-timestamp \\
      > \"\$root/client.log\" 2>&1 < /dev/null &
    echo \$! > \"\$root/client.pid\"
  "
}

report() {
  printf '\n=== %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  k get pods \
    -o custom-columns='POD:.metadata.name,READY:.status.containerStatuses[*].ready,PHASE:.status.phase,RESTARTS:.status.containerStatuses[*].restartCount,NODE:.spec.nodeName' \
    2>&1 | grep -E 'POD|glm53-(franka2a|pcp8n|gb200-tp8)-dp8' || true

  local arm
  for arm in pcp8-dcp8-ep8 pcp8-ep8 tp8-ep8; do
    printf '%s summaries=%s client=' "$arm" "$(summary_count "$arm")"
    client_state "$arm" | head -1
    remote "tr '\\r' '\\n' < '$remote_root/$arm/client.log' 2>/dev/null | grep 'Processing\\[parallel_' | tail -1" 2>/dev/null || true
  done
}

while :; do
  {
    report
    release_completed_arm pcp8-dcp8-ep8 \
      lwilkinson-glm53-franka2a-dp8-prefill \
      lwilkinson-glm53-franka2a-dp8-decode
    release_completed_arm pcp8-ep8 \
      lwilkinson-glm53-pcp8n-dp8-prefill \
      lwilkinson-glm53-pcp8n-dp8-decode
    start_tp_client
  } >> "$local_root/monitor.log" 2>&1
  sleep "$interval"
done
