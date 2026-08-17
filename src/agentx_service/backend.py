from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

from .models import AttemptPhase, BenchmarkRequest, OperatorConfig


@dataclass(frozen=True)
class JobObservation:
    phase: AttemptPhase
    admitted_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None


class JobBackend(Protocol):
    def create(self, namespace: str, manifest: dict[str, Any]) -> None: ...
    def observe(self, namespace: str, name: str) -> JobObservation: ...
    def delete(self, namespace: str, name: str) -> None: ...
    def list_owned(self, namespace: str) -> list[str]: ...


def build_job_manifest(
    *,
    run_id: str,
    concurrency: int,
    attempt: int,
    request: BenchmarkRequest,
    config: OperatorConfig,
    attempt_directory: str,
) -> dict[str, Any]:
    """Render a fixed argv Job; no caller-controlled URL, image, path, or CLI fragment."""
    target = config.targets[request.logical_model_target]
    queue = config.queues[request.local_queue]
    monitoring = config.monitoring_profiles[request.monitoring_profile]
    name = f"agentx-{run_id[:10]}-c{concurrency}-a{attempt}"
    args = [
        "profile",
        "--scenario",
        "inferencex-agentx-mvp",
        "--url",
        target.endpoint_url,
        "--model",
        request.served_model_name,
        "--max-context-length",
        str(config.max_context_length),
        "--endpoint-type",
        "chat",
        "--streaming",
        "--use-server-token-count",
        "--public-dataset",
        "semianalysis_cc_traces_weka_with_subagents",
        "--concurrency",
        str(concurrency),
        "--benchmark-duration",
        str(request.duration_seconds),
        "--server-metrics",
        monitoring.server_metrics_url,
        "--gpu-telemetry",
        *monitoring.gpu_telemetry_urls,
        "--output-artifact-dir",
        attempt_directory,
        "--ui",
        "simple",
    ]
    if request.duration_seconds < config.limits.scenario_valid_duration_seconds:
        # This fixed service-owned flag enables bounded plumbing smoke tests;
        # the report continues to mark the result scenario-invalid.
        args.append("--unsafe-override")
    container: dict[str, Any] = {
        "name": "aiperf",
        "image": config.aiperf_image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["/opt/venv/bin/aiperf"],
        "args": args,
        "env": [
            {"name": "AIPERF_DATASET_WEKA_LIVE_ASSISTANT_RESPONSES", "value": "1"},
            {"name": "HF_HOME", "value": "/workspace/.cache/huggingface"},
        ],
        "resources": {
            "requests": {"cpu": queue.cpu_request, "memory": queue.memory_request},
            "limits": {
                "cpu": queue.cpu_limit,
                "memory": queue.memory_limit,
                "ephemeral-storage": queue.ephemeral_storage_limit,
            },
        },
        "volumeMounts": [
            {"name": "workspace", "mountPath": "/workspace"},
            {"name": "artifacts", "mountPath": config.storage.mount_path},
        ],
    }
    if config.hf_token_secret_name:
        container["envFrom"] = [
            {"secretRef": {"name": config.hf_token_secret_name, "optional": True}}
        ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "labels": {
                "app.kubernetes.io/name": "agentx-benchmark",
                "app.kubernetes.io/managed-by": "agentx-service",
                "agentx.inference.dev/run-id": run_id,
                "agentx.inference.dev/result-label": request.result_label,
                "kueue.x-k8s.io/queue-name": request.local_queue,
            },
        },
        "spec": {
            "suspend": True,
            "backoffLimit": 0,
            "activeDeadlineSeconds": request.duration_seconds
            + config.limits.runtime_grace_seconds,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "agentx-benchmark",
                        "agentx.inference.dev/run-id": run_id,
                    }
                },
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [container],
                    "volumes": [
                        {"name": "workspace", "emptyDir": {"sizeLimit": "20Gi"}},
                        {
                            "name": "artifacts",
                            "persistentVolumeClaim": {
                                "claimName": config.storage.pvc_name
                            },
                        },
                    ],
                },
            },
        },
    }


class KubectlBackend:
    def _run(self, args: list[str], *, stdin: str | None = None) -> str:
        completed = subprocess.run(
            ["kubectl", *args],
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()[-2000:]
            raise RuntimeError(f"kubectl {' '.join(args[:4])} failed: {detail}")
        return completed.stdout

    def create(self, namespace: str, manifest: dict[str, Any]) -> None:
        self._run(["apply", "-n", namespace, "-f", "-"], stdin=json.dumps(manifest))

    def observe(self, namespace: str, name: str) -> JobObservation:
        value = json.loads(
            self._run(["get", "job", name, "-n", namespace, "-o", "json"])
        )
        status = value.get("status", {})
        conditions = {item.get("type"): item for item in status.get("conditions", [])}
        if conditions.get("Complete", {}).get("status") == "True":
            return JobObservation(
                AttemptPhase.SUCCEEDED,
                started_at=status.get("startTime"),
                completed_at=status.get("completionTime"),
            )
        failed = conditions.get("Failed", {})
        if failed.get("status") == "True":
            return JobObservation(
                AttemptPhase.FAILED,
                started_at=status.get("startTime"),
                completed_at=failed.get("lastTransitionTime"),
                error=f"{failed.get('reason', 'JobFailed')}: {failed.get('message', 'benchmark Job failed')}",
            )
        if value.get("spec", {}).get("suspend", False):
            return JobObservation(AttemptPhase.ADMISSION_PENDING)
        admitted = status.get("startTime")
        if status.get("active", 0):
            return JobObservation(
                AttemptPhase.RUNNING, admitted_at=admitted, started_at=admitted
            )
        return JobObservation(AttemptPhase.RUNTIME_PENDING, admitted_at=admitted)

    def delete(self, namespace: str, name: str) -> None:
        self._run(
            [
                "delete",
                "job",
                name,
                "-n",
                namespace,
                "--ignore-not-found=true",
                "--wait=false",
            ]
        )

    def list_owned(self, namespace: str) -> list[str]:
        value = json.loads(
            self._run(
                [
                    "get",
                    "jobs",
                    "-n",
                    namespace,
                    "-l",
                    "app.kubernetes.io/managed-by=agentx-service",
                    "-o",
                    "json",
                ]
            )
        )
        return [item["metadata"]["name"] for item in value.get("items", [])]
