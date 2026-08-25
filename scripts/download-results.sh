#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh
load_agentx_env
require_agentx_env KUBE_CONTEXT NAMESPACE RESULTS_PVC

helper="agentx-results-$(date -u +%Y%m%d%H%M%S)"
manifest="$(mktemp)"
trap 'k delete pod "$helper" --ignore-not-found >/dev/null 2>&1 || true; rm -f "$manifest"' EXIT

cat >"$manifest" <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${helper}
  namespace: ${NAMESPACE}
spec:
  restartPolicy: Never
  containers:
    - name: results
      image: busybox:1.37
      command: [sh, -c, "sleep 3600"]
      volumeMounts:
        - name: results
          mountPath: /results
  volumes:
    - name: results
      persistentVolumeClaim:
        claimName: ${RESULTS_PVC}
EOF

k apply -f "$manifest" >/dev/null
k wait --for=condition=Ready "pod/$helper" --timeout=2m >/dev/null
mkdir -p results
k cp "$helper:/results/$RESULTS_PREFIX/." results/
echo "Downloaded to $(pwd)/results"
