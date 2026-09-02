#!/usr/bin/env bash
set -euo pipefail

readonly context=default
readonly namespace=vllm
readonly pod=lwilkinson-vllm-devbox
readonly manifest=deploy/gb200-devbox.yaml
action="${1:-status}"

case "$action" in
  up)
    kubectl --context "$context" apply -f "$manifest"
    kubectl --context "$context" -n "$namespace" wait "pod/$pod" \
      --for=condition=Ready --timeout=10m
    ;;
  status)
    kubectl --context "$context" -n "$namespace" get "pod/$pod" -o wide
    ;;
  shell)
    exec kubectl --context "$context" -n "$namespace" exec -it "$pod" -- bash
    ;;
  down)
    kubectl --context "$context" delete -f "$manifest" --ignore-not-found
    ;;
  *)
    echo "usage: $0 {up|status|shell|down}" >&2
    exit 2
    ;;
esac
