#!/usr/bin/env bash
set -euo pipefail

readonly kube_context=default
readonly namespace=vllm
readonly listen_port=1080

if ! lsof -nP -iTCP:"$listen_port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "ERROR: no SOCKS listener on TCP $listen_port; run 'just gb200-tunnel' in another terminal." >&2
  exit 2
fi

echo "Local active context (unchanged): $(kubectl config current-context)"
echo "Querying GB200 with explicit context: $kube_context"
echo

kubectl --context "$kube_context" get nodes -o wide
echo

pod_scope=(-n "$namespace")
scope_label="namespace $namespace"
if [[ "$(kubectl --context "$kube_context" auth can-i list pods --all-namespaces 2>/dev/null)" == "yes" ]]; then
  pod_scope=(--all-namespaces)
  scope_label="all namespaces"
else
  echo "NOTE: RBAC cannot list pods cluster-wide; GPU requests below cover only namespace '$namespace'."
  echo "      Unrequested-in-scope GPUs are not guaranteed to be free cluster-wide."
  echo
fi

jq -r -s --arg scope "$scope_label" '
  .[0].items as $nodes |
  .[1].items as $pods |
  [ $pods[]
    | select(.status.phase != "Succeeded" and .status.phase != "Failed")
    | ([.spec.containers[]?
        | (.resources.requests["nvidia.com/gpu"]
           // .resources.limits["nvidia.com/gpu"] // "0")
        | tonumber] | add // 0) as $gpu
    | select($gpu > 0)
    | {namespace: .metadata.namespace, pod: .metadata.name,
       node: (.spec.nodeName // "<pending>"), gpu: $gpu,
       phase: .status.phase}
  ] as $gpu_pods |
  [ $nodes[]
    | select(.status.allocatable["nvidia.com/gpu"] != null)
    | .metadata.name as $node
    | (.status.allocatable["nvidia.com/gpu"] | tonumber) as $capacity
    | ([$gpu_pods[] | select(.node == $node) | .gpu] | add // 0) as $requested
    | {node: $node, capacity: $capacity, requested: $requested,
       unrequested: ($capacity - $requested)}
  ] as $gpu_nodes |
  "GPU nodes: \($gpu_nodes | length)",
  "Allocatable GPU capacity: \($gpu_nodes | map(.capacity) | add // 0)",
  "Active GPU requests visible in \($scope): \($gpu_pods | map(.gpu) | add // 0)",
  "Unrequested GPUs visible in scope: \($gpu_nodes | map(.unrequested) | add // 0)",
  "Nodes with no visible GPU request: \($gpu_nodes | map(select(.requested == 0)) | length)",
  "",
  "NAMESPACE\tPOD\tNODE\tGPU\tPHASE",
  ($gpu_pods[] | "\(.namespace)\t\(.pod)\t\(.node)\t\(.gpu)\t\(.phase)")
' \
  <(kubectl --context "$kube_context" get nodes -o json) \
  <(kubectl --context "$kube_context" "${pod_scope[@]}" get pods -o json)
