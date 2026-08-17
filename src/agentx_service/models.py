from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

StrictName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?$")
]
RunId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{24}$")]
ServedModelName = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"
    ),
]
ResultLabel = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=63,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?$",
    ),
]
WorkstreamId = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$"
    ),
]
Concurrency = Annotated[int, Field(strict=True, ge=1, le=2048)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BenchmarkRequest(StrictModel):
    logical_model_target: StrictName
    served_model_name: ServedModelName
    scenario: Literal["agentx-mvp"] = "agentx-mvp"
    concurrencies: list[Concurrency] = Field(
        min_length=1, max_length=8, json_schema_extra={"uniqueItems": True}
    )
    duration_seconds: int = Field(strict=True, ge=60, le=7200)
    retries: int = Field(default=2, strict=True, ge=0, le=5)
    monitoring_profile: StrictName
    local_queue: StrictName
    result_label: ResultLabel
    vdp_workstream_id: WorkstreamId | None = None

    @field_validator("concurrencies")
    @classmethod
    def validate_concurrencies(cls, value: list[int]) -> list[int]:
        if any(isinstance(item, bool) or item < 1 or item > 2048 for item in value):
            raise ValueError("concurrency values must be integers from 1 through 2048")
        if len(set(value)) != len(value):
            raise ValueError("concurrency values must be unique")
        return value


def _operator_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError(
            "operator URL must be an absolute HTTP(S) URL without credentials or fragments"
        )
    return value.rstrip("/")


class TargetConfig(StrictModel):
    endpoint_url: str
    served_model_names: list[ServedModelName] = Field(min_length=1, max_length=16)
    allowed_queues: list[StrictName] = Field(min_length=1, max_length=16)
    model_revision: str = Field(
        min_length=7, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"
    )
    vllm_image: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^[^\s@]+@sha256:[0-9a-f]{64}$",
    )
    vllm_fingerprint: str | None = Field(default=None, max_length=256)

    @field_validator("endpoint_url")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        return _operator_url(value)

    @field_validator("served_model_names", "allowed_queues")
    @classmethod
    def unique_values(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("values must be unique")
        return value


class QueueConfig(StrictModel):
    namespace: StrictName
    cpu_request: str = Field(pattern=r"^[1-9][0-9]*m?$", max_length=16)
    cpu_limit: str = Field(pattern=r"^[1-9][0-9]*m?$", max_length=16)
    memory_request: str = Field(pattern=r"^[1-9][0-9]*(?:Mi|Gi)$", max_length=16)
    memory_limit: str = Field(pattern=r"^[1-9][0-9]*(?:Mi|Gi)$", max_length=16)
    ephemeral_storage_request: str = Field(
        default="20Gi", pattern=r"^[1-9][0-9]*(?:Mi|Gi)$", max_length=16
    )
    ephemeral_storage_limit: str = Field(
        default="20Gi", pattern=r"^[1-9][0-9]*(?:Mi|Gi)$", max_length=16
    )


class MonitoringConfig(StrictModel):
    server_metrics_url: str
    gpu_telemetry_urls: list[str] = Field(min_length=1, max_length=64)
    prometheus_url: str
    grafana_url: str
    server_up_query: str = Field(min_length=1, max_length=2048)
    gpu_up_query: str = Field(min_length=1, max_length=2048)
    expected_server_targets: int = Field(strict=True, ge=1, le=4096)
    expected_gpu_targets: int = Field(strict=True, ge=1, le=4096)
    capture_queries: dict[StrictName, str] = Field(min_length=1, max_length=32)

    @field_validator("server_metrics_url", "prometheus_url", "grafana_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return _operator_url(value)

    @field_validator("gpu_telemetry_urls")
    @classmethod
    def valid_urls(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("GPU telemetry URLs must be unique")
        return [_operator_url(value) for value in values]


class StorageConfig(StrictModel):
    pvc_name: StrictName
    mount_path: str = Field(pattern=r"^/[A-Za-z0-9._/-]+$", max_length=256)
    runs_subdirectory: str = Field(
        default="runs", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", max_length=128
    )

    @model_validator(mode="after")
    def safe_paths(self) -> StorageConfig:
        path = Path(self.mount_path)
        if (
            self.mount_path == "/"
            or ".." in path.parts
            or ".." in Path(self.runs_subdirectory).parts
        ):
            raise ValueError(
                "storage paths must be absolute, scoped, and contain no '..'"
            )
        return self


class LimitsConfig(StrictModel):
    admission_timeout_seconds: int = Field(default=1800, ge=60, le=86400)
    runtime_grace_seconds: int = Field(default=1800, ge=60, le=14400)
    maximum_active_runs: int = Field(default=8, ge=1, le=128)
    maximum_list_results: int = Field(default=100, ge=1, le=500)
    maximum_mcp_result_bytes: int = Field(default=1_048_576, ge=4096, le=4_194_304)
    maximum_profile_bytes: int = Field(default=16_777_216, ge=1024, le=134_217_728)
    maximum_artifact_files: int = Field(default=500, ge=10, le=10_000)
    maximum_artifact_file_bytes: int = Field(
        default=536_870_912, ge=1024, le=4_294_967_296
    )
    maximum_attempt_artifact_bytes: int = Field(
        default=2_147_483_648, ge=4096, le=17_179_869_184
    )
    maximum_log_bytes: int = Field(default=16_777_216, ge=1024, le=134_217_728)
    maximum_retained_terminal_runs: int = Field(default=50, ge=1, le=500)
    terminal_retention_seconds: int = Field(default=604_800, ge=3600, le=31_536_000)
    scenario_valid_duration_seconds: int = Field(default=900, ge=60, le=7200)


class OperatorConfig(StrictModel):
    service_namespace: StrictName
    aiperf_image: str = Field(min_length=1, max_length=512)
    hf_token_secret_name: StrictName | None = None
    max_context_length: int = Field(default=131072, ge=1024, le=1_048_576)
    targets: dict[StrictName, TargetConfig] = Field(min_length=1, max_length=64)
    queues: dict[StrictName, QueueConfig] = Field(min_length=1, max_length=32)
    monitoring_profiles: dict[StrictName, MonitoringConfig] = Field(
        min_length=1, max_length=32
    )
    storage: StorageConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    @model_validator(mode="after")
    def coherent_limits(self) -> OperatorConfig:
        if self.limits.maximum_active_runs > self.limits.maximum_list_results:
            raise ValueError(
                "maximum_list_results must cover every possible active run for restart reconstruction"
            )
        mismatched = sorted(
            name
            for name, queue in self.queues.items()
            if queue.namespace != self.service_namespace
        )
        if mismatched:
            raise ValueError(
                "all LocalQueues must be in service_namespace because Jobs and the PVC are namespaced: "
                + ", ".join(mismatched)
            )
        return self


class BenchmarkPlan(StrictModel):
    mutates_state: Literal[False] = False
    request: BenchmarkRequest
    effective_target: dict[str, Any]
    queue_resource_coverage: dict[str, Any]
    job_count: int
    maximum_attempt_count: int
    estimated_deadline_seconds: int
    storage_destination: str
    monitoring_sources: dict[str, Any]
    scenario_validity: dict[str, Any]


class AttemptPhase(StrEnum):
    ADMISSION_PENDING = "admission_pending"
    RUNTIME_PENDING = "runtime_pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class AttemptRecord(StrictModel):
    concurrency: int
    attempt: int
    job_name: str
    phase: AttemptPhase = AttemptPhase.ADMISSION_PENDING
    submitted_at: str
    admitted_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    measurement_start: str | None = None
    measurement_end: str | None = None
    attempt_artifact_directory: str
    canonical_artifact_directory: str
    error: str | None = None
    cleanup_pending: bool = False
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    monitoring_warnings: list[str] = Field(default_factory=list)
    monitoring_provenance: dict[str, Any] = Field(default_factory=dict)


class RunState(StrEnum):
    ADMISSION_PENDING = "admission_pending"
    RUNTIME_PENDING = "runtime_pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELED = "canceled"


class BenchmarkRecord(StrictModel):
    run_id: RunId
    state: RunState
    request: BenchmarkRequest
    plan: BenchmarkPlan
    created_at: str
    updated_at: str
    attempts: list[AttemptRecord] = Field(default_factory=list)
    completed_concurrencies: list[int] = Field(default_factory=list)
    failed_concurrencies: list[int] = Field(default_factory=list)
    active_concurrency_index: int = 0
    terminal_error: str | None = None
    vdp_workstream_id: WorkstreamId | None = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def epoch_ns_to_iso(value: Any) -> str | None:
    try:
        seconds = float(value) / 1_000_000_000
        return (
            datetime.fromtimestamp(seconds, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return None
