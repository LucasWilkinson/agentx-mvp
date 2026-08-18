from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from agentx_service.backend import (
    JobNotFound,
    JobObservation,
    KubectlBackend,
    QueueObservation,
    build_job_manifest,
)
from agentx_service.cli import _submit_http
from agentx_service.controller import BenchmarkController
from agentx_service.mcp import (
    LEGACY_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    AgentXMcp,
)
from agentx_service.models import (
    AttemptPhase,
    AttemptRecord,
    BenchmarkRequest,
    OperatorConfig,
    RunState,
)
from agentx_service.monitoring import PrometheusMonitoring
from agentx_service.planner import PlanningError, plan_benchmark
from agentx_service.server import authorized
from agentx_service.store import FileRunStore


class FakeBackend:
    def __init__(self):
        self.manifests = []
        self.observations = {}
        self.deleted = []
        self.delete_failures = {}
        self.delete_pending = {}
        self.create_failure = None

    def create(self, namespace, manifest):
        self.manifests.append((namespace, manifest))
        self.observations.setdefault(
            manifest["metadata"]["name"], JobObservation(AttemptPhase.ADMISSION_PENDING)
        )
        if self.create_failure is not None:
            failure = self.create_failure
            self.create_failure = None
            raise failure

    def observe(self, namespace, name):
        value = self.observations[name]
        if isinstance(value, Exception):
            raise value
        return value

    def delete(self, namespace, name):
        failure = self.delete_failures.pop(name, None)
        if failure is not None:
            raise failure
        self.deleted.append((namespace, name))
        remaining = self.delete_pending.get(name, 0)
        if remaining:
            self.delete_pending[name] = remaining - 1
            return False
        return True

    def list_owned(self, namespace):
        return list(self.observations)

    def inspect_queue(self, namespace, name):
        return QueueObservation(
            local_queue=name,
            namespace=namespace,
            cluster_queue="a100-benchmark",
            active=True,
            covered_resources=("cpu", "ephemeral-storage", "memory"),
            flavors_by_resource={
                "cpu": ("a100",),
                "memory": ("a100",),
                "ephemeral-storage": ("a100",),
            },
            nominal_quota_by_resource={
                "cpu": ("128",),
                "memory": ("1Ti",),
                "ephemeral-storage": ("1Ti",),
            },
        )

    def logs(self, namespace, name, maximum_bytes):
        return b"aiperf completed\n"[:maximum_bytes]

    def ready(self, namespace):
        return {"kubernetes": "ok", "namespace": namespace}


class FakeMonitoring:
    def __init__(self):
        self.fail_capture = False

    def preflight(self, target, profile, served_model_name):
        return {
            "target_models": list(target.served_model_names),
            "server_targets": {"expected": 1, "observed": 1},
            "gpu_targets": {"expected": 1, "observed": 1},
        }

    def capture(self, profile, start, end, destination):
        if self.fail_capture:
            raise RuntimeError("Prometheus unavailable")
        value = {"exact_window": {"start": start, "end": end}, "queries": {}}
        (destination / "prometheus_export.json").write_text(json.dumps(value))
        return value

    def ready(self, profile):
        return {"prometheus": "ok"}

    def warmup(self, target, served_model_name):
        return {
            "started_at": "2026-08-17T00:00:00Z",
            "completed_at": "2026-08-17T00:00:01Z",
            "system_fingerprint": "fp-warmup",
        }


def operator_config(root: str, **limit_overrides) -> OperatorConfig:
    limits = {
        "admission_timeout_seconds": 1800,
        "runtime_grace_seconds": 1800,
        "maximum_active_runs": 8,
        "maximum_list_results": 100,
        "maximum_mcp_result_bytes": 1048576,
        "scenario_valid_duration_seconds": 900,
        **limit_overrides,
    }
    return OperatorConfig.model_validate(
        {
            "service_namespace": "bench",
            "aiperf_image": "registry.test/aiperf:fixed",
            "hf_token_secret_name": "hf-token",
            "max_context_length": 131072,
            "targets": {
                "kimi-k3-a100": {
                    "endpoint_url": "http://gateway.models.svc.cluster.local/v1",
                    "served_model_names": ["mgoin/Kimi-K3-pruned75"],
                    "allowed_queues": ["a100-benchmark"],
                    "model_revision": "3c8e22fdac0c14409eebd48ba78c10a940f5267a",
                    "vllm_image": "registry.test/vllm@sha256:" + "a" * 64,
                    "vllm_fingerprint": "fp-operator",
                }
            },
            "queues": {
                "a100-benchmark": {
                    "namespace": "bench",
                    "cpu_request": "4",
                    "cpu_limit": "16",
                    "memory_request": "8Gi",
                    "memory_limit": "32Gi",
                    "ephemeral_storage_request": "20Gi",
                    "ephemeral_storage_limit": "20Gi",
                }
            },
            "monitoring_profiles": {
                "full": {
                    "server_metrics_url": "http://gateway.models.svc.cluster.local/metrics",
                    "gpu_telemetry_urls": [
                        "http://dcgm.monitoring.svc.cluster.local:9400/metrics"
                    ],
                    "prometheus_url": "http://prometheus.monitoring.svc.cluster.local",
                    "grafana_url": "http://grafana.monitoring.svc.cluster.local",
                    "server_up_query": 'up{job="vllm"}',
                    "gpu_up_query": 'up{job="dcgm"}',
                    "expected_server_targets": 1,
                    "expected_gpu_targets": 1,
                    "capture_queries": {"vllm": "vllm:num_requests_running"},
                }
            },
            "storage": {
                "pvc_name": "results",
                "mount_path": root,
                "runs_subdirectory": "runs",
            },
            "limits": limits,
        }
    )


def request(**overrides) -> BenchmarkRequest:
    value = {
        "logical_model_target": "kimi-k3-a100",
        "served_model_name": "mgoin/Kimi-K3-pruned75",
        "scenario": "agentx-mvp",
        "concurrencies": [1],
        "duration_seconds": 60,
        "retries": 1,
        "monitoring_profile": "full",
        "local_queue": "a100-benchmark",
        "result_label": "test-run",
    }
    value.update(overrides)
    return BenchmarkRequest.model_validate(value)


def mcp_message(method, params=None, *, version=PROTOCOL_VERSION):
    values = dict(params or {})
    values["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    }
    headers = {
        "content-type": "application/json",
        "mcp-protocol-version": version,
        "mcp-method": method,
    }
    if method == "tools/call":
        headers["mcp-name"] = values.get("name", "")
    return headers, {"jsonrpc": "2.0", "id": 1, "method": method, "params": values}


def legacy_mcp_message(method, params=None, *, request_id=1):
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if method != "initialize":
        headers["mcp-protocol-version"] = LEGACY_PROTOCOL_VERSION
    message = {"jsonrpc": "2.0", "method": method, "params": dict(params or {})}
    if request_id is not None:
        message["id"] = request_id
    return headers, message


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = operator_config(self.temp.name)
        self.backend = FakeBackend()
        self.monitoring = FakeMonitoring()
        self.store = FileRunStore(Path(self.temp.name) / "runs")
        self.controller = BenchmarkController(
            self.config, self.backend, self.store, self.monitoring
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_shipped_kimi_config_matches_deployment_storage(self):
        root = Path(__file__).resolve().parents[1]
        shipped = OperatorConfig.model_validate_json(
            (root / "examples/operator-config.kimi-k3-a100.json").read_text()
        )
        deployment = (root / "deploy/agentx-service.yaml").read_text()
        justfile = (root / "Justfile").read_text()
        service_dockerfile = (root / "Dockerfile.agentx-service").read_text()
        self.assertIn(f"claimName: {shipped.storage.pvc_name}", deployment)
        self.assertIn(f"mountPath: {shipped.storage.mount_path}", deployment)
        self.assertNotIn("lustre", deployment.lower())
        self.assertNotIn("namespace: vllm", deployment)
        self.assertIn('--serviceaccount="{{NAMESPACE}}:agentx-service"', justfile)
        self.assertIn("agentx-service-clusterqueue-reader-{{NAMESPACE}}", justfile)
        self.assertIn("agentx-service-build:", justfile)
        self.assertIn("USER 10001:10001", service_dockerfile)
        self.assertIn("readOnlyRootFilesystem: true", deployment)

    def write_artifacts(self, record, *, monitoring=True):
        attempt = record.attempts[-1]
        path = Path(attempt.attempt_artifact_directory)
        path.mkdir(parents=True, exist_ok=True)
        (path / "profile_export_aiperf.json").write_text(
            json.dumps(
                {
                    "min_request_timestamp": {"avg": 1_700_000_000_000_000_000},
                    "max_response_timestamp": {"avg": 1_700_000_060_000_000_000},
                    "request_throughput": {"avg": 2.5, "unit": "req/s"},
                    "inter_token_latency": {"p90": 0.04, "unit": "s"},
                }
            )
        )
        (path / "vllm_fingerprint.txt").write_text("fp-runtime\n")
        if monitoring:
            (path / "server_metrics_export.json").write_text("{}")
            (path / "gpu_telemetry_export.jsonl").write_text("{}\n")

    def test_request_validation_denies_unbounded_and_unsafe_inputs(self):
        with self.assertRaises(ValidationError):
            request(concurrencies=list(range(1, 10)))
        with self.assertRaises(ValidationError):
            request(concurrencies=[1, 1])
        with self.assertRaises(ValidationError):
            request(concurrencies=[True])
        with self.assertRaises(ValidationError):
            request(concurrencies=["1"])
        with self.assertRaises(ValidationError):
            BenchmarkRequest.model_validate(
                {
                    **request().model_dump(),
                    "url": "http://attacker",
                    "image": "bad",
                    "shell": "rm -rf /",
                }
            )

    def test_operator_config_rejects_cross_namespace_queues(self):
        value = self.config.model_dump(mode="json")
        value["service_namespace"] = "other"
        with self.assertRaisesRegex(ValidationError, "service_namespace"):
            OperatorConfig.model_validate(value)

    def test_operator_config_requires_digest_pinned_vllm_image(self):
        value = self.config.model_dump(mode="json")
        value["targets"]["kimi-k3-a100"]["vllm_image"] = (
            "quay.io/vllm/vllm-openai:latest"
        )
        with self.assertRaisesRegex(ValidationError, "vllm_image"):
            OperatorConfig.model_validate(value)

    def test_monitoring_preflight_rejects_duplicate_unhealthy_or_unidentified_targets(
        self,
    ):
        def result(*items):
            return {"data": {"result": list(items)}}

        healthy = {
            "metric": {"pod": "server-0", "endpoint": "metrics"},
            "value": [1, "1"],
        }
        self.assertEqual(
            PrometheusMonitoring._validate_targets(result(healthy), 1, "server")[
                "observed"
            ],
            1,
        )
        with self.assertRaisesRegex(RuntimeError, "duplicates"):
            PrometheusMonitoring._validate_targets(
                result(healthy, healthy), 2, "server"
            )
        with self.assertRaisesRegex(RuntimeError, "unhealthy"):
            PrometheusMonitoring._validate_targets(
                result({**healthy, "value": [1, "0"]}), 1, "server"
            )
        with self.assertRaisesRegex(RuntimeError, "unidentified"):
            PrometheusMonitoring._validate_targets(
                result({"metric": {}, "value": [1, "1"]}), 1, "server"
            )

    def test_plan_is_non_mutating_and_shows_required_fields(self):
        plan = self.controller.plan(request(concurrencies=[1, 64]))
        self.assertFalse(plan.mutates_state)
        self.assertEqual(plan.job_count, 2)
        self.assertEqual(plan.maximum_attempt_count, 4)
        self.assertEqual(plan.queue_resource_coverage["missing"], [])
        self.assertIn("<run-id>", plan.storage_destination)
        self.assertIn("prometheus", plan.monitoring_sources)
        self.assertEqual(self.store.list(), [])

    def test_plan_rejects_unknown_target_queue_model_and_missing_coverage(self):
        with self.assertRaises(PlanningError):
            self.controller.plan(request(local_queue="other"))
        with self.assertRaises(PlanningError):
            self.controller.plan(request(served_model_name="attacker/model"))
        observed = self.backend.inspect_queue("bench", "a100-benchmark")
        broken = QueueObservation(
            **{**observed.__dict__, "covered_resources": ("cpu", "ephemeral-storage")}
        )
        with self.assertRaisesRegex(PlanningError, "memory"):
            plan_benchmark(request(), self.config, broken)

    def test_job_is_kueue_gated_cpu_only_and_has_fixed_argv(self):
        manifest = build_job_manifest(
            run_id="a" * 24,
            concurrency=64,
            attempt=1,
            request=request(concurrencies=[64]),
            config=self.config,
            attempt_directory=f"{self.temp.name}/runs/{'a' * 24}/attempts/c64/a1",
        )
        self.assertTrue(manifest["spec"]["suspend"])
        self.assertEqual(
            manifest["metadata"]["labels"]["kueue.x-k8s.io/queue-name"],
            "a100-benchmark",
        )
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["/bin/bash"])
        self.assertNotIn("nvidia.com/gpu", json.dumps(container["resources"]))
        self.assertEqual(
            container["resources"]["requests"]["ephemeral-storage"], "20Gi"
        )
        self.assertIn("--unsafe-override", container["args"])
        self.assertNotIn("rm ", container["args"][1])
        self.assertIn("/bounded-artifacts", container["args"][1])
        self.assertIn("ARTIFACT_MAX_FILES", container["args"][1])
        env = {item["name"]: item["value"] for item in container["env"]}
        mounts = {item["name"]: item for item in container["volumeMounts"]}
        self.assertEqual(env["ARTIFACT_MAX_FILES"], "500")
        self.assertEqual(env["ARTIFACT_DEST"], "/agentx-output")
        self.assertEqual(env["HOME"], "/workspace")
        self.assertEqual(env["XDG_CACHE_HOME"], "/workspace/.cache")
        self.assertEqual(env["HF_HOME"], "/workspace/.cache/huggingface")
        self.assertEqual(mounts["artifacts"]["mountPath"], "/agentx-output")
        self.assertEqual(
            mounts["artifacts"]["subPath"],
            f"runs/{'a' * 24}/attempts/c64/a1",
        )
        self.assertNotIn(
            self.config.storage.mount_path,
            {item["mountPath"] for item in mounts.values()},
        )
        self.assertFalse(
            manifest["spec"]["template"]["spec"]["automountServiceAccountToken"]
        )

    def test_kubectl_log_capture_streams_with_a_server_side_byte_limit(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"x" * 128)
        os.close(write_fd)

        class Process:
            stdout = os.fdopen(read_fd, "rb", buffering=0)

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

        with patch(
            "agentx_service.backend.subprocess.Popen", return_value=Process()
        ) as popen:
            value = KubectlBackend().logs("bench", "job", 64)
        self.assertLessEqual(len(value), 64)
        self.assertIn(b"truncated", value)
        self.assertIn("--limit-bytes=64", popen.call_args.args[0])

    def test_kubectl_job_submission_uses_create_not_apply(self):
        calls = []

        class Backend(KubectlBackend):
            def _run(self, args, *, stdin=None):
                calls.append((args, stdin))
                return ""

        Backend().create("bench", {"kind": "Job", "metadata": {"name": "job"}})
        self.assertEqual(calls[0][0], ["create", "-n", "bench", "-f", "-"])
        self.assertEqual(json.loads(calls[0][1])["metadata"]["name"], "job")

    def test_kubectl_cleanup_waits_for_both_job_and_pods_to_disappear(self):
        calls = []

        class Backend(KubectlBackend):
            def _run(self, args, *, stdin=None):
                calls.append(args)
                if args[:2] == ["get", "pods"]:
                    return "pod/terminating\n"
                return ""

        self.assertFalse(Backend().delete("bench", "job"))
        self.assertEqual(calls[0][:3], ["delete", "job", "job"])
        self.assertIn(["get", "pods"], [call[:2] for call in calls])

    def test_shipped_rbac_is_limited_to_exercised_operations(self):
        root = Path(__file__).resolve().parents[1]
        deployment = (root / "deploy/agentx-service.yaml").read_text()
        self.assertIn('verbs: ["create", "get", "list", "delete"]', deployment)
        self.assertIn('resources: ["pods"]\n    verbs: ["list"]', deployment)
        self.assertIn('resources: ["localqueues"]\n    verbs: ["get"]', deployment)
        self.assertIn('resources: ["workloads"]\n    verbs: ["list"]', deployment)
        self.assertNotIn('"patch"', deployment)
        self.assertNotIn('"watch"', deployment)

    def test_kubectl_readiness_uses_supported_get_flags(self):
        calls = []

        class Backend(KubectlBackend):
            def _run(self, args, *, stdin=None):
                calls.append(args)
                return ""

        self.assertEqual(
            Backend().ready("bench"),
            {"kubernetes": "ok", "namespace": "bench"},
        )
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--limit=1", calls[0])
        self.assertEqual(calls[0][:4], ["get", "jobs", "-n", "bench"])

    def test_kueue_workload_conditions_are_returned_as_admission_diagnostics(self):
        class Backend(KubectlBackend):
            def _run(self, args, *, stdin=None):
                return json.dumps(
                    {
                        "items": [
                            {
                                "status": {
                                    "conditions": [
                                        {
                                            "type": "QuotaReserved",
                                            "status": "False",
                                            "reason": "Pending",
                                            "message": "insufficient a100 flavor quota",
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )

        detail = Backend()._workload_diagnostic("bench", "job", "uid")
        self.assertIn("insufficient a100 flavor quota", detail)

    def test_failed_job_includes_pod_termination_diagnostics(self):
        class Backend(KubectlBackend):
            def _run(self, args, *, stdin=None):
                if args[:2] == ["get", "job"]:
                    return json.dumps(
                        {
                            "status": {
                                "startTime": "2026-08-17T00:00:00Z",
                                "conditions": [
                                    {
                                        "type": "Failed",
                                        "status": "True",
                                        "reason": "BackoffLimitExceeded",
                                        "message": "Job has reached the backoff limit",
                                    }
                                ],
                            }
                        }
                    )
                return json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "job-pod"},
                                "status": {
                                    "reason": "Failed",
                                    "containerStatuses": [
                                        {
                                            "name": "aiperf",
                                            "state": {
                                                "terminated": {
                                                    "reason": "OOMKilled",
                                                    "exitCode": 137,
                                                }
                                            },
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                )

        observation = Backend().observe("bench", "job")
        self.assertEqual(observation.phase, AttemptPhase.FAILED)
        self.assertEqual(observation.admitted_at, "2026-08-17T00:00:00Z")
        self.assertIn("BackoffLimitExceeded", observation.error)
        self.assertIn("OOMKilled (exit 137)", observation.error)

    def test_completed_job_backfills_admission_when_running_phase_was_missed(self):
        class Backend(KubectlBackend):
            def _run(self, args, *, stdin=None):
                return json.dumps(
                    {
                        "status": {
                            "startTime": "2026-08-17T00:00:00Z",
                            "completionTime": "2026-08-17T00:01:00Z",
                            "conditions": [{"type": "Complete", "status": "True"}],
                        }
                    }
                )

        observation = Backend().observe("bench", "job")
        self.assertEqual(observation.phase, AttemptPhase.SUCCEEDED)
        self.assertEqual(observation.admitted_at, "2026-08-17T00:00:00Z")
        self.assertEqual(observation.started_at, "2026-08-17T00:00:00Z")

    def test_admission_and_runtime_are_distinct(self):
        record = self.controller.submit(request())
        self.assertEqual(record.state, RunState.ADMISSION_PENDING)
        job = record.attempts[-1].job_name
        self.backend.observations[job] = JobObservation(
            AttemptPhase.RUNTIME_PENDING, admitted_at="2026-08-17T00:00:00Z"
        )
        record = self.controller.reconcile(record.run_id)
        self.assertEqual(record.state, RunState.RUNTIME_PENDING)
        self.backend.observations[job] = JobObservation(
            AttemptPhase.RUNNING, started_at="2026-08-17T00:01:00Z"
        )
        self.assertEqual(
            self.controller.reconcile(record.run_id).state, RunState.RUNNING
        )

    def test_ambiguous_job_create_reconciles_same_deterministic_attempt(self):
        self.backend.create_failure = RuntimeError("kubectl timed out after send")
        record = self.controller.submit(request())
        self.assertEqual(len(record.attempts), 1)
        self.assertEqual(len(self.backend.manifests), 1)
        self.assertIn("uncertain", record.terminal_error)
        reconciled = self.controller.reconcile(record.run_id)
        self.assertEqual(len(reconciled.attempts), 1)
        self.assertEqual(len(self.backend.manifests), 1)
        self.assertEqual(reconciled.state, RunState.ADMISSION_PENDING)

    def test_retry_then_success_promotes_artifacts_and_exact_window(self):
        record = self.controller.submit(request(retries=1))
        first = record.attempts[-1].job_name
        self.backend.observations[first] = JobObservation(
            AttemptPhase.FAILED, error="pod evicted"
        )
        record = self.controller.reconcile(record.run_id)
        self.assertEqual(len(record.attempts), 2)
        self.assertEqual(record.attempts[0].error, "pod evicted")
        self.write_artifacts(record)
        second = record.attempts[-1].job_name
        self.backend.observations[second] = JobObservation(
            AttemptPhase.SUCCEEDED, completed_at="2026-08-17T00:03:00Z"
        )
        record = self.controller.reconcile(record.run_id)
        self.assertEqual(record.state, RunState.COMPLETED)
        successful = record.attempts[-1]
        self.assertEqual(successful.measurement_start, "2023-11-14T22:13:20.000Z")
        self.assertEqual(successful.measurement_end, "2023-11-14T22:14:20.000Z")
        self.assertIn("profile_export_aiperf.json", successful.artifact_hashes)
        dashboard = Path(successful.canonical_artifact_directory) / "dashboard.html"
        self.assertIn("request_throughput", dashboard.read_text())
        sweep = (
            Path(self.temp.name)
            / "runs"
            / record.run_id
            / "interactivity_vs_throughput.html"
        )
        self.assertIn("const points=", sweep.read_text())
        report = self.controller.get_report(record.run_id)
        self.assertEqual(report["vllm"]["fingerprint"], "fp-runtime")
        self.assertEqual(report["vllm"]["fingerprint_source"], "aiperf")

    def test_runtime_fingerprint_source_wins_even_when_operator_value_matches(self):
        value = self.config.model_dump(mode="json")
        value["targets"]["kimi-k3-a100"]["vllm_fingerprint"] = "fp-runtime"
        controller = BenchmarkController(
            OperatorConfig.model_validate(value),
            self.backend,
            self.store,
            self.monitoring,
        )
        record = controller.submit(request())
        self.write_artifacts(record)
        self.backend.observations[record.attempts[-1].job_name] = JobObservation(
            AttemptPhase.SUCCEEDED
        )
        record = controller.reconcile(record.run_id)
        report = controller.get_report(record.run_id)
        self.assertEqual(report["vllm"]["fingerprint"], "fp-runtime")
        self.assertEqual(report["vllm"]["fingerprint_source"], "aiperf")

    def test_partial_sweep_continues_after_terminal_concurrency_failure(self):
        record = self.controller.submit(request(concurrencies=[1, 2], retries=0))
        self.backend.observations[record.attempts[-1].job_name] = JobObservation(
            AttemptPhase.FAILED, error="bad one"
        )
        record = self.controller.reconcile(record.run_id)
        self.assertEqual(record.failed_concurrencies, [1])
        self.assertEqual(record.attempts[-1].concurrency, 2)
        self.write_artifacts(record)
        self.backend.observations[record.attempts[-1].job_name] = JobObservation(
            AttemptPhase.SUCCEEDED
        )
        record = self.controller.reconcile(record.run_id)
        self.assertEqual(record.state, RunState.PARTIAL)
        self.assertIn("failed concurrency", record.terminal_error)

    def test_artifact_failure_retries_and_reports_actionable_error(self):
        record = self.controller.submit(request(retries=0))
        Path(record.attempts[-1].attempt_artifact_directory).mkdir(
            parents=True, exist_ok=True
        )
        self.backend.observations[record.attempts[-1].job_name] = JobObservation(
            AttemptPhase.SUCCEEDED
        )
        record = self.controller.reconcile(record.run_id)
        self.assertEqual(record.state, RunState.FAILED)
        self.assertIn("profile_export_aiperf.json", record.attempts[0].error)

    def test_cancellation_is_idempotent_and_deletes_active_job(self):
        record = self.controller.submit(request())
        canceled = self.controller.cancel(record.run_id)
        self.assertEqual(canceled.state, RunState.CANCELED)
        self.assertEqual(len(self.backend.deleted), 1)
        self.controller.cancel(record.run_id)
        self.assertEqual(len(self.backend.deleted), 1)

    def test_restart_reconstructs_nonterminal_run(self):
        record = self.controller.submit(request())
        job = record.attempts[-1].job_name
        self.backend.observations[job] = JobObservation(
            AttemptPhase.RUNNING, started_at="2026-08-17T00:00:00Z"
        )
        restarted = BenchmarkController(
            self.config, self.backend, self.store, self.monitoring
        )
        recovered = restarted.reconstruct()
        self.assertEqual(recovered[0].state, RunState.RUNNING)

    def test_restart_recreates_a_persisted_missing_job(self):
        record = self.controller.submit(request())
        job = record.attempts[-1].job_name
        self.backend.observations[job] = JobNotFound("deleted")
        manifests_before = len(self.backend.manifests)
        recovered = self.controller.reconstruct()[0]
        self.assertEqual(recovered.state, RunState.ADMISSION_PENDING)
        self.assertEqual(len(self.backend.manifests), manifests_before + 1)
        self.assertIn("reconstructed", recovered.terminal_error)

    def test_restart_finishes_persisted_success_cleanup_without_rerunning(self):
        record = self.controller.submit(request())
        self.write_artifacts(record)
        job = record.attempts[-1].job_name
        self.backend.observations[job] = JobObservation(AttemptPhase.SUCCEEDED)
        self.backend.delete_failures[job] = RuntimeError("API unavailable")
        pending = self.controller.reconcile(record.run_id)
        self.assertTrue(pending.attempts[-1].cleanup_pending)
        manifests = len(self.backend.manifests)
        recovered = self.controller.reconstruct()[0]
        self.assertEqual(recovered.state, RunState.COMPLETED)
        self.assertFalse(recovered.attempts[-1].cleanup_pending)
        self.assertEqual(len(self.backend.manifests), manifests)

    def test_restart_finishes_persisted_cancel_cleanup_without_rerunning(self):
        record = self.controller.submit(request())
        job = record.attempts[-1].job_name
        self.backend.delete_failures[job] = RuntimeError("API unavailable")
        pending = self.controller.cancel(record.run_id)
        self.assertTrue(pending.attempts[-1].cleanup_pending)
        manifests = len(self.backend.manifests)
        recovered = self.controller.reconstruct()[0]
        self.assertEqual(recovered.state, RunState.CANCELED)
        self.assertFalse(recovered.attempts[-1].cleanup_pending)
        self.assertEqual(len(self.backend.manifests), manifests)

    def test_cleanup_pending_cancellation_counts_against_active_capacity(self):
        limited = operator_config(self.temp.name, maximum_active_runs=1)
        controller = BenchmarkController(
            limited, self.backend, self.store, self.monitoring
        )
        record = controller.submit(request())
        job = record.attempts[-1].job_name
        self.backend.delete_failures[job] = RuntimeError("API unavailable")
        pending = controller.cancel(record.run_id)
        self.assertTrue(pending.attempts[-1].cleanup_pending)
        with self.assertRaisesRegex(RuntimeError, "maximum active"):
            controller.submit(request(result_label="second"))

    def test_cleanup_stays_pending_until_job_and_pods_are_absent(self):
        limited = operator_config(self.temp.name, maximum_active_runs=1)
        controller = BenchmarkController(
            limited, self.backend, self.store, self.monitoring
        )
        record = controller.submit(request())
        job = record.attempts[-1].job_name
        self.backend.delete_pending[job] = 1
        pending = controller.cancel(record.run_id)
        self.assertTrue(pending.attempts[-1].cleanup_pending)
        with self.assertRaisesRegex(RuntimeError, "maximum active"):
            controller.submit(request(result_label="blocked"))
        complete = controller.cancel(record.run_id)
        self.assertFalse(complete.attempts[-1].cleanup_pending)

    def test_plan_fails_when_live_clusterqueue_is_inactive(self):
        observed = self.backend.inspect_queue("bench", "a100-benchmark")
        self.backend.inspect_queue = lambda _namespace, _name: QueueObservation(
            **{**observed.__dict__, "active": False}
        )
        with self.assertRaisesRegex(PlanningError, "not Active"):
            self.controller.plan(request())

    def test_monitoring_preflight_requires_the_requested_model(self):
        value = self.config.model_dump(mode="json")
        value["targets"]["kimi-k3-a100"]["served_model_names"].append("other/model")
        configured = OperatorConfig.model_validate(value)
        monitor = PrometheusMonitoring()
        with (
            patch.object(
                monitor,
                "_json",
                return_value={"data": [{"id": "other/model"}]},
            ),
            self.assertRaisesRegex(RuntimeError, "requested model"),
        ):
            monitor.preflight(
                configured.targets["kimi-k3-a100"],
                configured.monitoring_profiles["full"],
                "mgoin/Kimi-K3-pruned75",
            )

    def test_plan_requires_one_flavor_with_sufficient_full_podset_quota(self):
        observed = self.backend.inspect_queue("bench", "a100-benchmark")
        insufficient = QueueObservation(
            **{
                **observed.__dict__,
                "nominal_quota_by_resource": {
                    **observed.nominal_quota_by_resource,
                    "ephemeral-storage": ("1Gi",),
                },
            }
        )
        self.backend.inspect_queue = lambda _namespace, _name: insufficient
        with self.assertRaisesRegex(PlanningError, "full PodSet"):
            self.controller.plan(request())

    def test_monitoring_failures_are_visible_without_losing_results(self):
        record = self.controller.submit(request())
        self.write_artifacts(record, monitoring=False)
        self.backend.observations[record.attempts[-1].job_name] = JobObservation(
            AttemptPhase.SUCCEEDED
        )
        record = self.controller.reconcile(record.run_id)
        report = self.controller.get_report(record.run_id)
        self.assertEqual(record.state, RunState.COMPLETED)
        self.assertEqual(len(report["monitoring_provenance"]["warnings"]), 2)
        self.assertEqual(report["per_concurrency"][0]["vllm_fingerprint"], "fp-runtime")

    def test_prometheus_capture_failure_is_a_durable_warning(self):
        record = self.controller.submit(request())
        self.write_artifacts(record)
        self.monitoring.fail_capture = True
        self.backend.observations[record.attempts[-1].job_name] = JobObservation(
            AttemptPhase.SUCCEEDED
        )
        record = self.controller.reconcile(record.run_id)
        self.assertEqual(record.state, RunState.COMPLETED)
        self.assertTrue(
            any(
                "Prometheus capture failed" in item
                for item in record.attempts[-1].monitoring_warnings
            )
        )

    def test_oversized_artifacts_are_removed_and_attempt_fails(self):
        limited = operator_config(
            self.temp.name,
            maximum_artifact_file_bytes=1024,
            maximum_attempt_artifact_bytes=4096,
        )
        controller = BenchmarkController(
            limited, self.backend, self.store, self.monitoring
        )
        record = controller.submit(request(retries=0))
        self.write_artifacts(record)
        path = Path(record.attempts[-1].attempt_artifact_directory)
        (path / "huge.log").write_bytes(b"x" * 2048)
        self.backend.observations[record.attempts[-1].job_name] = JobObservation(
            AttemptPhase.SUCCEEDED
        )
        record = controller.reconcile(record.run_id)
        self.assertEqual(record.state, RunState.FAILED)
        self.assertTrue((path / "artifact-limit-error.json").is_file())
        self.assertFalse((path / "huge.log").exists())

    def test_canonical_promotion_rejects_artifacts_from_a_different_retry(self):
        record = self.controller.submit(request())
        self.write_artifacts(record)
        first = record.attempts[-1]
        self.controller._promote_artifacts(record, first)
        second_path = Path(first.attempt_artifact_directory).with_name("a2")
        second_path.mkdir(parents=True)
        profile = json.loads(
            (
                Path(first.attempt_artifact_directory) / "profile_export_aiperf.json"
            ).read_text()
        )
        (second_path / "profile_export_aiperf.json").write_text(json.dumps(profile))
        (second_path / "vllm_fingerprint.txt").write_text("fp-runtime\n")
        (second_path / "server_metrics_export.json").write_text("{}")
        (second_path / "gpu_telemetry_export.jsonl").write_text("{}\n")
        second = AttemptRecord(
            concurrency=first.concurrency,
            attempt=2,
            job_name="agentx-retry",
            submitted_at=first.submitted_at,
            attempt_artifact_directory=str(second_path),
            canonical_artifact_directory=first.canonical_artifact_directory,
        )
        with self.assertRaisesRegex(RuntimeError, "do not match"):
            self.controller._promote_artifacts(record, second)

    def test_canonical_promotion_rehashes_existing_files(self):
        record = self.controller.submit(request())
        self.write_artifacts(record)
        attempt = record.attempts[-1]
        self.controller._promote_artifacts(record, attempt)
        canonical = Path(attempt.canonical_artifact_directory)
        (canonical / "profile_export_aiperf.json").write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "hash does not match"):
            self.controller._promote_artifacts(record, attempt)

    def test_postprocessing_failure_does_not_poison_retry_promotion(self):
        record = self.controller.submit(request(retries=1))
        self.write_artifacts(record)
        first = record.attempts[-1]
        self.backend.observations[first.job_name] = JobObservation(
            AttemptPhase.SUCCEEDED
        )
        with patch(
            "agentx_service.controller.write_dashboard",
            side_effect=OSError("transient dashboard write failure"),
        ):
            record = self.controller.reconcile(record.run_id)
        self.assertFalse(Path(first.canonical_artifact_directory).exists())
        self.assertEqual(len(record.attempts), 2)
        self.write_artifacts(record)
        second = record.attempts[-1]
        self.backend.observations[second.job_name] = JobObservation(
            AttemptPhase.SUCCEEDED
        )
        record = self.controller.reconcile(record.run_id)
        self.assertEqual(record.state, RunState.COMPLETED)

    def test_retention_waits_for_run_scoped_readers(self):
        limited = operator_config(self.temp.name, maximum_retained_terminal_runs=1)
        controller = BenchmarkController(
            limited, self.backend, self.store, self.monitoring
        )
        first = controller.submit(request(result_label="first"))
        with controller._run_lock(first.run_id):
            first = controller._cancel(first.run_id)
        time.sleep(0.002)
        second = controller.submit(request(result_label="second"))
        with controller._run_lock(second.run_id):
            controller._cancel(second.run_id)

        started = threading.Event()

        def retain():
            started.set()
            controller.enforce_retention()

        with controller._run_lock(first.run_id):
            thread = threading.Thread(target=retain)
            thread.start()
            started.wait(timeout=1)
            time.sleep(0.01)
            self.assertIsNotNone(self.store.get(first.run_id))
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.store.get(first.run_id))

    def test_artifact_listing_reports_truncation(self):
        limited = operator_config(self.temp.name, maximum_artifact_files=10)
        controller = BenchmarkController(
            limited, self.backend, self.store, self.monitoring
        )
        record = controller.submit(request())
        path = Path(record.attempts[-1].attempt_artifact_directory)
        path.mkdir(parents=True, exist_ok=True)
        for index in range(12):
            (path / f"artifact-{index}.txt").write_text(str(index))
        listing = controller.list_artifacts(record.run_id)
        self.assertTrue(listing["truncated"])
        self.assertEqual(listing["total_files"], 12)
        self.assertEqual(len(listing["artifacts"]), 10)

    def test_readiness_checks_storage_kubernetes_queue_and_monitoring(self):
        detail = self.controller.readiness()
        self.assertEqual(detail["storage"], "writable")
        self.assertEqual(detail["kubernetes"], "ok")
        self.assertTrue(detail["queues"]["a100-benchmark"])
        self.assertEqual(detail["prometheus"], "ok")

    def test_terminal_run_retention_removes_oldest_state_and_artifacts(self):
        limited = operator_config(self.temp.name, maximum_retained_terminal_runs=1)
        controller = BenchmarkController(
            limited, self.backend, self.store, self.monitoring
        )
        first = controller.cancel(controller.submit(request()).run_id)
        time.sleep(0.002)
        second = controller.cancel(controller.submit(request()).run_id)
        self.assertIsNone(self.store.get(first.run_id))
        self.assertIsNotNone(self.store.get(second.run_id))

    def test_mcp_is_stateless_strict_bounded_and_correctly_annotated(self):
        mcp = AgentXMcp(self.controller)
        headers, body = mcp_message("tools/list")
        status, response = mcp.handle(headers, body)
        self.assertEqual(status, 200)
        tools = {item["name"]: item for item in response["result"]["tools"]}
        self.assertEqual(
            set(tools),
            {
                "plan_agentx_benchmark",
                "submit_agentx_benchmark",
                "list_agentx_benchmarks",
                "get_agentx_benchmark",
                "cancel_agentx_benchmark",
                "list_agentx_artifacts",
                "get_agentx_report",
            },
        )
        self.assertTrue(tools["plan_agentx_benchmark"]["annotations"]["readOnlyHint"])
        self.assertTrue(
            tools["cancel_agentx_benchmark"]["annotations"]["destructiveHint"]
        )
        self.assertFalse(
            tools["submit_agentx_benchmark"]["annotations"]["idempotentHint"]
        )
        concurrency_schema = tools["plan_agentx_benchmark"]["inputSchema"][
            "properties"
        ]["concurrencies"]
        self.assertTrue(concurrency_schema["uniqueItems"])
        self.assertEqual(concurrency_schema["items"]["minimum"], 1)
        self.assertEqual(concurrency_schema["items"]["maximum"], 2048)
        self.assertNotIn("mcp-session-id", json.dumps(response).lower())
        headers, body = mcp_message("tools/list", version="2025-06-18")
        self.assertEqual(mcp.handle(headers, body)[1]["error"]["code"], -32022)

    def test_mcp_2025_11_25_initialize_list_call_and_notification(self):
        mcp = AgentXMcp(self.controller)
        headers, body = legacy_mcp_message(
            "initialize",
            {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "conformance-test", "version": "1.0"},
            },
        )
        status, response = mcp.handle(headers, body)
        self.assertEqual(status, 200)
        self.assertEqual(response["result"]["protocolVersion"], LEGACY_PROTOCOL_VERSION)
        self.assertEqual(
            response["result"]["capabilities"]["tools"], {"listChanged": False}
        )
        self.assertNotIn("resultType", response["result"])

        headers, body = legacy_mcp_message("notifications/initialized", request_id=None)
        self.assertEqual(mcp.handle(headers, body), (202, None))

        headers, body = legacy_mcp_message("tools/list")
        status, response = mcp.handle(headers, body)
        self.assertEqual(status, 200)
        self.assertEqual(len(response["result"]["tools"]), 7)
        self.assertNotIn("ttlMs", response["result"])

        headers, body = legacy_mcp_message(
            "tools/call",
            {
                "name": "plan_agentx_benchmark",
                "arguments": request().model_dump(mode="json"),
            },
        )
        status, response = mcp.handle(headers, body)
        self.assertEqual(status, 200)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["job_count"], 1)

    def test_mcp_2025_11_25_negotiates_and_supports_ping(self):
        mcp = AgentXMcp(self.controller)
        headers, body = legacy_mcp_message(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "old", "version": "1"},
            },
        )
        status, response = mcp.handle(headers, body)
        self.assertEqual(status, 200)
        self.assertEqual(response["result"]["protocolVersion"], LEGACY_PROTOCOL_VERSION)

        headers, body = legacy_mcp_message("ping")
        self.assertEqual(mcp.handle(headers, body)[1]["result"], {})

    def test_http_bearer_authentication_fails_closed(self):
        self.assertTrue(authorized("Bearer secret", "secret"))
        self.assertFalse(authorized(None, "secret"))
        self.assertFalse(authorized("Bearer wrong", "secret"))

    def test_public_cli_submission_routes_through_authenticated_mcp(self):
        request_path = Path(self.temp.name) / "request.json"
        request_path.write_text(request().model_dump_json())

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _maximum):
                return b'{"jsonrpc":"2.0","id":1,"result":{}}'

        with (
            patch.dict(os.environ, {"AGENTX_API_TOKEN": "secret"}),
            patch("agentx_service.cli.urlopen", return_value=Response()) as urlopen,
            patch("builtins.print"),
        ):
            self.assertEqual(_submit_http(str(request_path)), 0)
        outbound = urlopen.call_args.args[0]
        self.assertEqual(outbound.get_header("Authorization"), "Bearer secret")
        body = json.loads(outbound.data)
        self.assertEqual(body["params"]["name"], "submit_agentx_benchmark")
        self.assertEqual(
            body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"],
            PROTOCOL_VERSION,
        )

    def test_public_cli_submission_fails_on_mcp_tool_error(self):
        request_path = Path(self.temp.name) / "request.json"
        request_path.write_text(request().model_dump_json())

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _maximum):
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "isError": True,
                            "structuredContent": {"error": "full"},
                        },
                    }
                ).encode()

        with (
            patch.dict(os.environ, {"AGENTX_API_TOKEN": "secret"}),
            patch("agentx_service.cli.urlopen", return_value=Response()),
            patch("builtins.print"),
        ):
            self.assertEqual(_submit_http(str(request_path)), 1)

    def test_mcp_plan_and_validation_error_are_tool_results(self):
        mcp = AgentXMcp(self.controller)
        headers, body = mcp_message(
            "tools/call",
            {
                "name": "plan_agentx_benchmark",
                "arguments": request().model_dump(mode="json"),
            },
        )
        status, response = mcp.handle(headers, body)
        self.assertEqual(status, 200)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["job_count"], 1)
        body["params"]["arguments"]["url"] = "http://attacker"
        response = mcp.handle(headers, body)[1]
        self.assertTrue(response["result"]["isError"])

    def test_mcp_replaces_oversized_tool_results_with_bounded_error(self):
        limited_config = operator_config(self.temp.name, maximum_mcp_result_bytes=4096)
        controller = BenchmarkController(
            limited_config, self.backend, self.store, self.monitoring
        )
        controller.get_report = lambda _run_id: {"raw": "x" * 5000}  # type: ignore[method-assign]
        mcp = AgentXMcp(controller)
        headers, body = mcp_message(
            "tools/call",
            {"name": "get_agentx_report", "arguments": {"run_id": "a" * 24}},
        )
        response = mcp.handle(headers, body)[1]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["maximum_bytes"], 4096)
        self.assertNotIn("x" * 100, json.dumps(response))

    def test_report_and_artifact_listing_are_hashed_and_bounded_by_type(self):
        record = self.controller.submit(request())
        self.write_artifacts(record)
        self.backend.observations[record.attempts[-1].job_name] = JobObservation(
            AttemptPhase.SUCCEEDED
        )
        record = self.controller.reconcile(record.run_id)
        listing = self.controller.list_artifacts(record.run_id)
        artifacts = listing["artifacts"]
        self.assertTrue(
            all(set(item) == {"name", "size_bytes", "sha256"} for item in artifacts)
        )
        report = self.controller.get_report(record.run_id)
        self.assertIn("artifact_hashes", report)
        self.assertFalse(report["scenario"]["valid"])


if __name__ == "__main__":
    unittest.main()
