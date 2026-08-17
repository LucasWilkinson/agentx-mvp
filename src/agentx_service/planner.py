from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath

from .backend import QueueObservation
from .models import BenchmarkPlan, BenchmarkRequest, OperatorConfig


class PlanningError(ValueError):
    pass


def _quantity(value: str) -> Decimal:
    suffixes = {
        "Ki": Decimal(1024),
        "Mi": Decimal(1024) ** 2,
        "Gi": Decimal(1024) ** 3,
        "Ti": Decimal(1024) ** 4,
        "Pi": Decimal(1024) ** 5,
        "Ei": Decimal(1024) ** 6,
        "n": Decimal("0.000000001"),
        "u": Decimal("0.000001"),
        "m": Decimal("0.001"),
        "k": Decimal(1000),
        "M": Decimal(1000) ** 2,
        "G": Decimal(1000) ** 3,
        "T": Decimal(1000) ** 4,
        "P": Decimal(1000) ** 5,
        "E": Decimal(1000) ** 6,
    }
    for suffix, multiplier in suffixes.items():
        if value.endswith(suffix):
            return Decimal(value[: -len(suffix)]) * multiplier
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise PlanningError(f"invalid Kubernetes resource quantity: {value}") from error


def plan_benchmark(
    request: BenchmarkRequest,
    config: OperatorConfig,
    queue_observation: QueueObservation,
    monitoring_status: dict[str, object] | None = None,
) -> BenchmarkPlan:
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

    if (
        queue_observation.local_queue != request.local_queue
        or queue_observation.namespace != queue.namespace
    ):
        raise PlanningError(
            "observed LocalQueue identity does not match operator configuration"
        )
    if not queue_observation.active:
        raise PlanningError(
            f"ClusterQueue {queue_observation.cluster_queue} is not Active"
        )
    required = {"cpu", "memory", "ephemeral-storage"}
    covered = set(queue_observation.covered_resources)
    missing = sorted(required - covered)
    if missing:
        raise PlanningError(
            f"LocalQueue does not cover required resources: {', '.join(missing)}"
        )
    requests = {
        "cpu": queue.cpu_request,
        "memory": queue.memory_request,
        "ephemeral-storage": queue.ephemeral_storage_request,
    }
    common_flavors = set.intersection(
        *(
            set(queue_observation.flavors_by_resource.get(resource, ()))
            for resource in sorted(required)
        )
    )
    eligible_flavors: list[str] = []
    for flavor in sorted(common_flavors):
        sufficient = True
        for resource, requested in requests.items():
            flavors = queue_observation.flavors_by_resource.get(resource, ())
            quotas = queue_observation.nominal_quota_by_resource.get(resource, ())
            try:
                quota = quotas[flavors.index(flavor)]
            except (ValueError, IndexError):
                sufficient = False
                break
            if _quantity(quota) < _quantity(requested):
                sufficient = False
                break
        if sufficient:
            eligible_flavors.append(flavor)
    if not eligible_flavors:
        raise PlanningError(
            "no single Kueue ResourceFlavor has nominal quota for the full PodSet request"
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
            "cluster_queue": queue_observation.cluster_queue,
            "cluster_queue_active": queue_observation.active,
            "required": sorted(required),
            "covered": sorted(covered),
            "missing": missing,
            "flavors_by_resource": queue_observation.flavors_by_resource,
            "nominal_quota_by_resource": queue_observation.nominal_quota_by_resource,
            "eligible_flavors": eligible_flavors,
            "per_job": {
                "requests": requests,
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
            "preflight": monitoring_status or {},
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
