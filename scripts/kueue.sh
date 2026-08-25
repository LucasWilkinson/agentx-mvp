#!/usr/bin/env bash
# Usage: kueue.sh install | status | uninstall
# Cluster-wide: installs the Kueue controller (kueue-system) plus the h200 ResourceFlavor and the
# `agentx` ClusterQueue, and the `agentx` LocalQueue in $NAMESPACE. Set KUEUE_QUEUE=agentx in .env
# afterwards so renders and aiperf Jobs carry the queue label.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE
version="${KUEUE_CHART_VERSION:-0.19.2}"
case "${1:-status}" in
  install)
    helm --kube-context "$KUBE_CONTEXT" upgrade --install kueue oci://registry.k8s.io/kueue/charts/kueue \
      --version "$version" -n kueue-system --create-namespace -f kueue/values.yaml --wait --timeout 5m
    kubectl --context "$KUBE_CONTEXT" apply -f kueue/resource-flavor.yaml -f kueue/cluster-queue.yaml
    k apply -f kueue/local-queue.yaml
    "$0" status
    ;;
  status)
    kubectl --context "$KUBE_CONTEXT" get clusterqueues,resourceflavors 2>&1
    echo; k get localqueues,workloads 2>&1
    ;;
  uninstall)
    k delete -f kueue/local-queue.yaml --ignore-not-found
    kubectl --context "$KUBE_CONTEXT" delete -f kueue/cluster-queue.yaml -f kueue/resource-flavor.yaml --ignore-not-found
    helm --kube-context "$KUBE_CONTEXT" uninstall kueue -n kueue-system
    ;;
  *) echo "usage: kueue.sh install|status|uninstall" >&2; exit 2 ;;
esac
