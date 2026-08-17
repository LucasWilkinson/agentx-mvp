from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .models import AttemptPhase, BenchmarkRequest, OperatorConfig


class JobNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class QueueObservation:
    local_queue: str
    namespace: str
    cluster_queue: str
    active: bool
    covered_resources: tuple[str, ...]
    flavors_by_resource: dict[str, tuple[str, ...]]
    nominal_quota_by_resource: dict[str, tuple[str, ...]]


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
    def inspect_queue(self, namespace: str, name: str) -> QueueObservation: ...
    def logs(self, namespace: str, name: str, maximum_bytes: int) -> bytes: ...
    def ready(self, namespace: str) -> dict[str, Any]: ...


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
        "/bounded-artifacts",
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
        "command": ["/bin/bash"],
        "args": [
            "-c",
            """set -uo pipefail
set +e
/opt/venv/bin/aiperf \"$@\"
status=$?
set -e
if [ -n \"$(find /bounded-artifacts -type l -print -quit)\" ]; then
  echo \"artifact symbolic links are forbidden\" >&2
  exit 90
fi
artifact_count=$(find /bounded-artifacts -type f -print | wc -l)
if [ \"$artifact_count\" -gt \"$ARTIFACT_MAX_FILES\" ]; then
  echo \"artifact file count exceeds the configured limit\" >&2
  exit 91
fi
if [ -n \"$(find /bounded-artifacts -type f -size +\"${ARTIFACT_MAX_FILE_BYTES}\"c -print -quit)\" ]; then
  echo \"artifact file exceeds the configured size limit\" >&2
  exit 92
fi
mkdir -p \"$ARTIFACT_DEST\"
cp -a /bounded-artifacts/. \"$ARTIFACT_DEST\"/
exit \"$status\"
""",
            "agentx-service-runner",
            *args,
        ],
        "env": [
            {"name": "AIPERF_DATASET_WEKA_LIVE_ASSISTANT_RESPONSES", "value": "1"},
            {"name": "HF_HOME", "value": "/workspace/.cache/huggingface"},
            {"name": "ARTIFACT_DEST", "value": attempt_directory},
            {
                "name": "ARTIFACT_MAX_FILES",
                "value": str(config.limits.maximum_artifact_files),
            },
            {
                "name": "ARTIFACT_MAX_FILE_BYTES",
                "value": str(config.limits.maximum_artifact_file_bytes),
            },
        ],
        "resources": {
            "requests": {
                "cpu": queue.cpu_request,
                "memory": queue.memory_request,
                "ephemeral-storage": queue.ephemeral_storage_request,
            },
            "limits": {
                "cpu": queue.cpu_limit,
                "memory": queue.memory_limit,
                "ephemeral-storage": queue.ephemeral_storage_limit,
            },
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": [
            {"name": "workspace", "mountPath": "/workspace"},
            {"name": "artifacts", "mountPath": config.storage.mount_path},
            {"name": "artifact-scratch", "mountPath": "/bounded-artifacts"},
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
                    "automountServiceAccountToken": False,
                    "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [container],
                    "volumes": [
                        {"name": "workspace", "emptyDir": {"sizeLimit": "20Gi"}},
                        {
                            "name": "artifacts",
                            "persistentVolumeClaim": {
                                "claimName": config.storage.pvc_name
                            },
                        },
                        {
                            "name": "artifact-scratch",
                            "emptyDir": {
                                "sizeLimit": str(
                                    config.limits.maximum_attempt_artifact_bytes
                                )
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
        try:
            value = json.loads(
                self._run(["get", "job", name, "-n", namespace, "-o", "json"])
            )
        except RuntimeError as error:
            if "not found" in str(error).lower():
                raise JobNotFound(f"Job {namespace}/{name} does not exist") from error
            raise
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
            return JobObservation(
                AttemptPhase.ADMISSION_PENDING,
                error=self._workload_diagnostic(
                    namespace,
                    name,
                    str(value.get("metadata", {}).get("uid", "")),
                ),
            )
        admitted = status.get("startTime")
        if status.get("active", 0):
            return JobObservation(
                AttemptPhase.RUNNING, admitted_at=admitted, started_at=admitted
            )
        return JobObservation(AttemptPhase.RUNTIME_PENDING, admitted_at=admitted)

    def _workload_diagnostic(
        self, namespace: str, job_name: str, job_uid: str
    ) -> str | None:
        if not job_uid:
            return None
        value = json.loads(
            self._run(
                [
                    "get",
                    "workloads.kueue.x-k8s.io",
                    "-n",
                    namespace,
                    "-l",
                    f"kueue.x-k8s.io/job-uid={job_uid}",
                    "-o",
                    "json",
                ]
            )
        )
        details: list[str] = []
        for workload in value.get("items", []):
            for condition in workload.get("status", {}).get("conditions", []):
                if condition.get("status") != "True" and not condition.get("message"):
                    continue
                detail = "/".join(
                    str(item)
                    for item in (
                        condition.get("type", "Condition"),
                        condition.get("status", "Unknown"),
                        condition.get("reason", "Unknown"),
                    )
                )
                message = str(condition.get("message", "")).strip()
                details.append(f"{detail}: {message}" if message else detail)
        if not details:
            return f"Kueue Workload for Job {job_name} has no admission condition yet"
        return "; ".join(details)[:2000]

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

    def inspect_queue(self, namespace: str, name: str) -> QueueObservation:
        local = json.loads(
            self._run(
                [
                    "get",
                    "localqueue.kueue.x-k8s.io",
                    name,
                    "-n",
                    namespace,
                    "-o",
                    "json",
                ]
            )
        )
        cluster_name = local.get("spec", {}).get("clusterQueue")
        if not isinstance(cluster_name, str) or not cluster_name:
            raise RuntimeError(f"LocalQueue {namespace}/{name} has no ClusterQueue")
        cluster = json.loads(
            self._run(
                ["get", "clusterqueue.kueue.x-k8s.io", cluster_name, "-o", "json"]
            )
        )
        active = any(
            item.get("type") == "Active" and item.get("status") == "True"
            for item in cluster.get("status", {}).get("conditions", [])
        )
        flavors: dict[str, list[str]] = {}
        quotas: dict[str, list[str]] = {}
        for group in cluster.get("spec", {}).get("resourceGroups", []):
            resources = [str(item) for item in group.get("coveredResources", [])]
            for resource in resources:
                flavors.setdefault(resource, [])
                quotas.setdefault(resource, [])
            for flavor in group.get("flavors", []):
                flavor_name = str(flavor.get("name", ""))
                for quota in flavor.get("resources", []):
                    resource = str(quota.get("name", ""))
                    if resource in resources:
                        flavors[resource].append(flavor_name)
                        quotas[resource].append(str(quota.get("nominalQuota", "")))
        return QueueObservation(
            local_queue=name,
            namespace=namespace,
            cluster_queue=cluster_name,
            active=active,
            covered_resources=tuple(sorted(flavors)),
            flavors_by_resource={key: tuple(value) for key, value in flavors.items()},
            nominal_quota_by_resource={
                key: tuple(value) for key, value in quotas.items()
            },
        )

    def logs(self, namespace: str, name: str, maximum_bytes: int) -> bytes:
        process = subprocess.Popen(
            [
                "kubectl",
                "logs",
                "-n",
                namespace,
                f"job/{name}",
                "--all-containers=true",
                f"--limit-bytes={maximum_bytes}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 30
        buffered = bytearray()
        timed_out = False
        while len(buffered) <= maximum_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                timed_out = True
                process.kill()
                break
            chunk = os.read(
                process.stdout.fileno(),
                min(65_536, maximum_bytes + 1 - len(buffered)),
            )
            if not chunk:
                break
            buffered.extend(chunk)
        selector.close()
        value = bytes(buffered)
        if len(value) > maximum_bytes:
            marker = b"\n[agentx-service: log truncated to configured limit]\n"
            value = value[: maximum_bytes - len(marker)] + marker
            process.kill()
        process.wait(timeout=5)
        if timed_out and not value:
            value = b"[agentx-service: kubectl logs timed out]\n"
        return value

    def ready(self, namespace: str) -> dict[str, Any]:
        self._run(
            [
                "get",
                "jobs",
                "-n",
                namespace,
                "-l",
                "app.kubernetes.io/managed-by=agentx-service",
                "--limit=1",
                "-o",
                "name",
            ]
        )
        return {"kubernetes": "ok", "namespace": namespace}
