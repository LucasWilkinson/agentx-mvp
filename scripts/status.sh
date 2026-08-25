#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh
load_agentx_env
require_agentx_env KUBE_CONTEXT NAMESPACE

k get jobs \
  -l app.kubernetes.io/name=agentx-aiperf \
  --sort-by=.metadata.creationTimestamp
