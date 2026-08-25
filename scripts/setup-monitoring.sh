#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh
load_agentx_env
require_agentx_env KUBE_CONTEXT NAMESPACE GRAFANA_ADMIN_USER GRAFANA_ADMIN_PASSWORD

command -v helm >/dev/null || { echo "ERROR: helm is required" >&2; exit 2; }
command -v kubectl >/dev/null || { echo "ERROR: kubectl is required" >&2; exit 2; }

values="$(mktemp)"
trap 'rm -f "$values"' EXIT
cat >"$values" <<EOF
alertmanager:
  enabled: false
prometheus-node-exporter:
  enabled: false
prometheus-pushgateway:
  enabled: false
kube-state-metrics:
  enabled: false
server:
  fullnameOverride: prometheus-server
  persistentVolume:
    enabled: false
  retention: 7d
extraScrapeConfigs: |
  - job_name: vllm-prefill
    scrape_interval: 5s
    scrape_timeout: 4s
    kubernetes_sd_configs:
      - role: pod
        namespaces:
          names: [${NAMESPACE}]
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_label_llm_d_ai_role]
        regex: prefill
        action: keep
      - source_labels: [__meta_kubernetes_pod_container_port_number]
        regex: "8000"
        action: keep
      - source_labels: [__meta_kubernetes_pod_name]
        target_label: pod
  - job_name: vllm-decode
    scrape_interval: 5s
    scrape_timeout: 4s
    kubernetes_sd_configs:
      - role: pod
        namespaces:
          names: [${NAMESPACE}]
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_label_llm_d_ai_role]
        regex: decode
        action: keep
      - source_labels: [__meta_kubernetes_pod_container_port_number]
        regex: "820[0-7]"
        action: keep
      - source_labels: [__meta_kubernetes_pod_name]
        target_label: pod
EOF

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts --force-update >/dev/null
helm repo update >/dev/null

helm upgrade --install agentx-prometheus prometheus-community/prometheus \
  --kube-context "$KUBE_CONTEXT" --namespace "$NAMESPACE" --create-namespace \
  --values "$values" --wait --timeout 10m

k create configmap glm52-grafana-dashboard \
  --from-file=glm52-agentx.json=dashboards/grafana-wideep-overview.json \
  --dry-run=client -o yaml | k apply -f -
k label configmap glm52-grafana-dashboard \
  grafana_dashboard=1 --overwrite

k create secret generic glm52-grafana-admin \
  --from-literal=admin-user="$GRAFANA_ADMIN_USER" \
  --from-literal=admin-password="$GRAFANA_ADMIN_PASSWORD" \
  --dry-run=client -o yaml | k apply -f -

helm upgrade --install agentx-grafana grafana/grafana \
  --kube-context "$KUBE_CONTEXT" --namespace "$NAMESPACE" \
  --set fullnameOverride=grafana \
  --set admin.existingSecret=glm52-grafana-admin \
  --set persistence.enabled=false \
  --set sidecar.dashboards.enabled=true \
  --set sidecar.dashboards.label=grafana_dashboard \
  --set datasources.'datasources\.yaml'.apiVersion=1 \
  --set datasources.'datasources\.yaml'.datasources[0].name=prometheus \
  --set datasources.'datasources\.yaml'.datasources[0].type=prometheus \
  --set datasources.'datasources\.yaml'.datasources[0].uid=PBFA97CFB590B2093 \
  --set datasources.'datasources\.yaml'.datasources[0].url=http://prometheus-server \
  --set datasources.'datasources\.yaml'.datasources[0].access=proxy \
  --set datasources.'datasources\.yaml'.datasources[0].isDefault=true \
  --wait --timeout 10m

echo "Monitoring is ready. Run: just monitor"
