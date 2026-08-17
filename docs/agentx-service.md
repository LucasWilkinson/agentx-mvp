# AgentX benchmark service

The service turns the AgentX-MVP AIPerf sweep into a bounded, durable
controller. Callers select operator-owned names; they cannot provide URLs,
images, paths, manifests, AIPerf arguments, or shell. Every benchmark is
planned before it is admitted as one suspended, CPU-only Kubernetes Job per
concurrency. Kueue owns unsuspension.

## Request schema

Both `plan_agentx_benchmark` and `submit_agentx_benchmark` accept exactly this
object (`additionalProperties: false`):

| Field | Type and bound |
| --- | --- |
| `logical_model_target` | DNS-like operator key, 1-63 characters |
| `served_model_name` | 1-160 characters, `[A-Za-z0-9._/-]` |
| `scenario` | exactly `agentx-mvp` |
| `concurrencies` | 1-8 unique integers, each 1-2048 |
| `duration_seconds` | integer, 60-7200; values below the operator validity minimum are smoke-only |
| `retries` | integer, 0-5; retries after the initial attempt |
| `monitoring_profile` | DNS-like operator key |
| `local_queue` | DNS-like operator key allowed for the selected target |
| `result_label` | Kubernetes-label-safe string, 1-63 characters |
| `vdp_workstream_id` | optional bounded identifier, 1-128 characters |

Unknown fields are errors. The other tool schemas are:

- `list_agentx_benchmarks`: optional `state` enum and `limit` 1-100.
- `get_agentx_benchmark`, `cancel_agentx_benchmark`,
  `list_agentx_artifacts`, and `get_agentx_report`: exactly one `run_id`, a
  24-character lowercase hexadecimal identifier.

`plan_agentx_benchmark` reports queue/resource coverage, the effective target,
Job and worst-case attempt counts, estimated deadline, durable destination,
monitoring sources, and scenario validity without writing state or contacting
Kubernetes.

## Operator configuration

The JSON document is strict and has these required sections:

- `aiperf_image`, optional `hf_token_secret_name`, and
  `max_context_length`.
- `targets`: logical keys mapped to `endpoint_url`, allowed
  `served_model_names`, allowed LocalQueues, pinned `model_revision`,
  `vllm_image`, and optional `vllm_fingerprint`.
- `queues`: LocalQueue keys mapped to namespace, CPU/memory request and limit,
  ephemeral storage limit, and covered resource names.
- `monitoring_profiles`: fixed server metrics, GPU telemetry, Prometheus, and
  Grafana URLs.
- `storage`: PVC name, absolute mount path, and scoped runs subdirectory.
- `limits`: admission timeout, runtime grace, active/list/MCP bounds, and the
  profile size bound, and scenario-valid duration.

See [the Kimi K3 A100 example](../examples/operator-config.kimi-k3-a100.json).
The service and AIPerf Jobs must mount the same PVC at the configured path.
Adjust the Deployment's PVC name and mount path when the operator document
differs. The PVC contains atomic state records, immutable attempt trees,
canonical successful artifacts, and reports, so a replacement Pod reconstructs
all non-terminal runs from Kubernetes.

## MCP contract

The only MCP endpoint is stateless `POST /mcp`, protocol `2026-07-28`.
Every request supplies per-request metadata and matching
`MCP-Protocol-Version`, `Mcp-Method`, and, for calls, `Mcp-Name` headers. There
is no `initialize`, session identifier, GET event stream, or legacy fallback.
Explicit browser origins are denied unless `AGENTX_ALLOWED_ORIGINS` names them.

The tools are:

- `plan_agentx_benchmark` (read-only, idempotent)
- `submit_agentx_benchmark` (mutating, non-idempotent)
- `list_agentx_benchmarks` (read-only, idempotent)
- `get_agentx_benchmark` (read-only, idempotent)
- `cancel_agentx_benchmark` (destructive, idempotent)
- `list_agentx_artifacts` (read-only, idempotent)
- `get_agentx_report` (read-only, idempotent)

Raw logs, JSONL request traces, Prometheus exports, and dashboards remain on
the PVC. MCP lists their bounded metadata and hashes but only returns the
bounded structured report.

## Deploy

Build `Dockerfile.orchestrator` with the image used by the Deployment, ensure
the results PVC exists, then:

```bash
export NAMESPACE=vllm
export AGENTX_OPERATOR_CONFIG=examples/operator-config.kimi-k3-a100.json
just agentx-service-deploy
kubectl -n "$NAMESPACE" get deploy,svc agentx-service
kubectl -n "$NAMESPACE" get role agentx-service -o yaml
```

The supplied Role can only create/read/patch/delete namespaced Jobs. Health is
`GET /healthz`; readiness is `GET /readyz`. Apply an environment-specific copy
of `deploy/network-policy.example.yaml` only after replacing its documented
selectors and API CIDR.

## Smoke and full requests

Plan the smoke request locally:

```bash
PYTHONPATH=src python3 -m agentx_service.cli \
  --config examples/operator-config.kimi-k3-a100.json \
  plan examples/kimi-k3-a100-smoke.json
```

The 60-second smoke result deliberately reports `scenario.valid: false`. The
full request uses two 900-second concurrency levels:

```bash
AGENTX_REQUEST=examples/kimi-k3-a100-full.json just orchestrator-run
```

An MCP plan call uses the matching service headers:

```bash
curl -sS http://agentx-service.vllm.svc.cluster.local:8080/mcp \
  -H 'content-type: application/json' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/call' \
  -H 'Mcp-Name: plan_agentx_benchmark' \
  --data-binary @examples/mcp-plan-kimi-k3-a100.json
```

## Lifecycle, reports, and migration

Admission pending, runtime pending, and running are separate phases. Failed
attempts retain their artifact directories. Only a successful attempt with a
valid AIPerf profile is atomically promoted. A failed concurrency does not
discard successful peers: the terminal state becomes `partial`. Cancellation
deletes only the active owned Job and is idempotent.

Reports contain AgentX scenario validity, interactivity/throughput metrics,
every attempt and exact unpadded measurement window, vLLM image/fingerprint,
server/GPU telemetry provenance and warnings, the effective request/target/
queue configuration, and SHA-256 artifact hashes. Existing
`gen_interactivity_chart.py`, Grafana export, dashboard overlay, and report
files remain unchanged and can operate on the canonical directories.

`just run` and `just orchestrator-run` now submit strict request JSON to the
same in-cluster typed controller. Existing scripts needing the old positional behavior can
temporarily use `just legacy-run` and `just legacy-orchestrator-run`; these are
compatibility paths, not agent-facing interfaces. `just sweep`, reporting, and
dashboard recipes are retained for historical result trees.
