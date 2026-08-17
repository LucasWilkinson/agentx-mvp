from __future__ import annotations

from pathlib import PurePosixPath

from .models import BenchmarkPlan, BenchmarkRequest, OperatorConfig


class PlanningError(ValueError):
    pass


def plan_benchmark(request: BenchmarkRequest, config: OperatorConfig) -> BenchmarkPlan:
    target = config.targets.get(request.logical_model_target)
    if target is None:
        raise PlanningError(
            f"logical model target is not operator configured: {request.logical_model_target}"
        )
    if request.served_model_name not in target.served_model_names:
        raise PlanningError(
            f"served model is not allowed for target {request.logical_model_target}"
        )
    queue = config.queues.get(request.local_queue)
    if queue is None:
        raise PlanningError(
            f"LocalQueue is not operator allowed: {request.local_queue}"
        )
    if request.local_queue not in target.allowed_queues:
        raise PlanningError(
            f"LocalQueue {request.local_queue} is not allowed for target {request.logical_model_target}"
        )
    monitoring = config.monitoring_profiles.get(request.monitoring_profile)
    if monitoring is None:
        raise PlanningError(
            f"monitoring profile is not operator configured: {request.monitoring_profile}"
        )

    required = {"cpu", "memory"}
    covered = set(queue.covered_resources)
    missing = sorted(required - covered)
    if missing:
        raise PlanningError(
            f"LocalQueue does not cover required resources: {', '.join(missing)}"
        )

    attempts = len(request.concurrencies) * (request.retries + 1)
    per_attempt = (
        config.limits.admission_timeout_seconds
        + request.duration_seconds
        + config.limits.runtime_grace_seconds
    )
    storage = (
        PurePosixPath(config.storage.mount_path)
        / config.storage.runs_subdirectory
        / "<run-id>"
    )
    return BenchmarkPlan(
        request=request,
        effective_target={
            "logical_name": request.logical_model_target,
            "endpoint_url": target.endpoint_url,
            "served_model_name": request.served_model_name,
            "model_revision": target.model_revision,
            "vllm_image": target.vllm_image,
            "vllm_fingerprint": target.vllm_fingerprint,
        },
        queue_resource_coverage={
            "local_queue": request.local_queue,
            "namespace": queue.namespace,
            "required": sorted(required),
            "covered": sorted(covered),
            "missing": missing,
            "per_job": {
                "requests": {"cpu": queue.cpu_request, "memory": queue.memory_request},
                "limits": {
                    "cpu": queue.cpu_limit,
                    "memory": queue.memory_limit,
                    "ephemeral-storage": queue.ephemeral_storage_limit,
                },
            },
        },
        job_count=len(request.concurrencies),
        maximum_attempt_count=attempts,
        estimated_deadline_seconds=attempts * per_attempt,
        storage_destination=str(storage),
        monitoring_sources={
            "profile": request.monitoring_profile,
            "server_metrics": monitoring.server_metrics_url,
            "gpu_telemetry": monitoring.gpu_telemetry_urls,
            "prometheus": monitoring.prometheus_url,
            "grafana": monitoring.grafana_url,
        },
        scenario_validity={
            "valid": request.duration_seconds
            >= config.limits.scenario_valid_duration_seconds,
            "minimum_duration_seconds": config.limits.scenario_valid_duration_seconds,
            "reason": None
            if request.duration_seconds >= config.limits.scenario_valid_duration_seconds
            else "duration is below the AgentX-MVP validity minimum (smoke only)",
        },
    )
