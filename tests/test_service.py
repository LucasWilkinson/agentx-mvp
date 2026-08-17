from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from agentx_service.backend import JobObservation, build_job_manifest
from agentx_service.controller import BenchmarkController
from agentx_service.mcp import PROTOCOL_VERSION, AgentXMcp
from agentx_service.models import (
    AttemptPhase,
    BenchmarkRequest,
    OperatorConfig,
    RunState,
)
from agentx_service.planner import PlanningError, plan_benchmark
from agentx_service.store import FileRunStore


class FakeBackend:
    def __init__(self):
        self.manifests = []
        self.observations = {}
        self.deleted = []

    def create(self, namespace, manifest):
        self.manifests.append((namespace, manifest))
        self.observations.setdefault(
            manifest["metadata"]["name"], JobObservation(AttemptPhase.ADMISSION_PENDING)
        )

    def observe(self, namespace, name):
        value = self.observations[name]
        if isinstance(value, Exception):
            raise value
        return value

    def delete(self, namespace, name):
        self.deleted.append((namespace, name))

    def list_owned(self, namespace):
        return list(self.observations)


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
                    "ephemeral_storage_limit": "20Gi",
                    "covered_resources": ["cpu", "memory"],
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


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = operator_config(self.temp.name)
        self.backend = FakeBackend()
        self.store = FileRunStore(Path(self.temp.name) / "runs")
        self.controller = BenchmarkController(self.config, self.backend, self.store)

    def tearDown(self):
        self.temp.cleanup()

    def write_artifacts(self, record, *, monitoring=True):
        attempt = record.attempts[-1]
        path = Path(attempt.attempt_artifact_directory)
        path.mkdir(parents=True)
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

    def test_plan_is_non_mutating_and_shows_required_fields(self):
        plan = plan_benchmark(request(concurrencies=[1, 64]), self.config)
        self.assertFalse(plan.mutates_state)
        self.assertEqual(plan.job_count, 2)
        self.assertEqual(plan.maximum_attempt_count, 4)
        self.assertEqual(plan.queue_resource_coverage["missing"], [])
        self.assertIn("<run-id>", plan.storage_destination)
        self.assertIn("prometheus", plan.monitoring_sources)
        self.assertEqual(self.store.list(), [])

    def test_plan_rejects_unknown_target_queue_model_and_missing_coverage(self):
        with self.assertRaises(PlanningError):
            plan_benchmark(request(local_queue="other"), self.config)
        with self.assertRaises(PlanningError):
            plan_benchmark(request(served_model_name="attacker/model"), self.config)
        broken = self.config.model_copy(deep=True)
        broken.queues["a100-benchmark"].covered_resources = ["cpu"]
        with self.assertRaisesRegex(PlanningError, "memory"):
            plan_benchmark(request(), broken)

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
        self.assertEqual(container["command"], ["/opt/venv/bin/aiperf"])
        self.assertNotIn("nvidia.com/gpu", json.dumps(container["resources"]))
        self.assertNotIn("bash", json.dumps(container))
        self.assertIn("--unsafe-override", container["args"])

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
        Path(record.attempts[-1].attempt_artifact_directory).mkdir(parents=True)
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
        restarted = BenchmarkController(self.config, self.backend, self.store)
        recovered = restarted.reconstruct()
        self.assertEqual(recovered[0].state, RunState.RUNNING)

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
        headers, body = mcp_message("initialize")
        self.assertEqual(mcp.handle(headers, body)[0], 404)
        headers, body = mcp_message("tools/list", version="2025-06-18")
        self.assertEqual(mcp.handle(headers, body)[1]["error"]["code"], -32022)

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
        controller = BenchmarkController(limited_config, self.backend, self.store)
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
        artifacts = self.controller.list_artifacts(record.run_id)
        self.assertTrue(
            all(set(item) == {"name", "size_bytes", "sha256"} for item in artifacts)
        )
        report = self.controller.get_report(record.run_id)
        self.assertIn("artifact_hashes", report)
        self.assertFalse(report["scenario"]["valid"])


if __name__ == "__main__":
    unittest.main()
