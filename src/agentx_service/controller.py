from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .backend import JobBackend, build_job_manifest
from .models import (
    AttemptPhase,
    AttemptRecord,
    BenchmarkRecord,
    BenchmarkRequest,
    OperatorConfig,
    RunState,
    epoch_ns_to_iso,
    utc_now,
)
from .planner import plan_benchmark
from .store import FileRunStore

TERMINAL_STATES = {
    RunState.COMPLETED,
    RunState.PARTIAL,
    RunState.FAILED,
    RunState.CANCELED,
}


class BenchmarkNotFound(KeyError):
    pass


class BenchmarkController:
    def __init__(
        self,
        config: OperatorConfig,
        backend: JobBackend,
        store: FileRunStore | None = None,
        *,
        now: Callable[[], str] = utc_now,
    ) -> None:
        self.config = config
        self.backend = backend
        root = Path(config.storage.mount_path) / config.storage.runs_subdirectory
        self.store = store or FileRunStore(root)
        self.now = now

    def plan(self, request: BenchmarkRequest):
        return plan_benchmark(request, self.config)

    def submit(self, request: BenchmarkRequest) -> BenchmarkRecord:
        active = [
            record
            for record in self.store.list(limit=self.config.limits.maximum_list_results)
            if record.state not in TERMINAL_STATES
        ]
        if len(active) >= self.config.limits.maximum_active_runs:
            raise RuntimeError("maximum active benchmark run count has been reached")
        plan = self.plan(request)
        created = self.now()
        record = BenchmarkRecord(
            run_id=secrets.token_hex(12),
            state=RunState.ADMISSION_PENDING,
            request=request,
            plan=plan,
            created_at=created,
            updated_at=created,
            vdp_workstream_id=request.vdp_workstream_id,
        )
        self.store.save(record)
        self._start_attempt(record, request.concurrencies[0], 1)
        return record

    def get(self, run_id: str) -> BenchmarkRecord:
        record = self.store.get(run_id)
        if record is None:
            raise BenchmarkNotFound(run_id)
        return record

    def list(
        self, *, state: str | None = None, limit: int = 100
    ) -> list[BenchmarkRecord]:
        return self.store.list(
            state=state, limit=min(limit, self.config.limits.maximum_list_results)
        )

    def cancel(self, run_id: str) -> BenchmarkRecord:
        record = self.get(run_id)
        if record.state in TERMINAL_STATES:
            return record
        active = self._active_attempt(record)
        if active is not None:
            queue = self.config.queues[record.request.local_queue]
            self.backend.delete(queue.namespace, active.job_name)
            active.phase = AttemptPhase.CANCELED
            active.completed_at = self.now()
            active.error = "canceled by request"
        record.state = RunState.CANCELED
        record.terminal_error = "benchmark canceled"
        record.updated_at = self.now()
        self.store.save(record)
        self._write_report(record)
        return record

    def reconstruct(self) -> list[BenchmarkRecord]:
        """Resume every PVC record after restart; Kubernetes remains authoritative."""
        recovered = []
        for record in self.store.list(limit=self.config.limits.maximum_list_results):
            if record.state not in TERMINAL_STATES:
                recovered.append(self.reconcile(record.run_id))
        return recovered

    def reconcile(self, run_id: str) -> BenchmarkRecord:
        record = self.get(run_id)
        if record.state in TERMINAL_STATES:
            return record
        attempt = self._active_attempt(record)
        if attempt is None:
            self._advance(record)
            return record
        queue = self.config.queues[record.request.local_queue]
        try:
            observation = self.backend.observe(queue.namespace, attempt.job_name)
        except Exception as error:  # noqa: BLE001 - API failures are transient.
            record.terminal_error = (
                f"Kubernetes observation failed (will retry): {error}"
            )
            record.updated_at = self.now()
            self.store.save(record)
            return record

        attempt.phase = observation.phase
        attempt.admitted_at = observation.admitted_at or attempt.admitted_at
        attempt.started_at = observation.started_at or attempt.started_at
        attempt.completed_at = observation.completed_at or attempt.completed_at
        attempt.error = observation.error
        record.terminal_error = None
        if observation.phase == AttemptPhase.ADMISSION_PENDING:
            record.state = RunState.ADMISSION_PENDING
            if (
                self._age_seconds(attempt.submitted_at)
                > self.config.limits.admission_timeout_seconds
            ):
                self.backend.delete(queue.namespace, attempt.job_name)
                self._fail_attempt(
                    record,
                    attempt,
                    "Kueue admission timed out; inspect LocalQueue quota and resource flavor coverage",
                )
        elif observation.phase == AttemptPhase.RUNTIME_PENDING:
            record.state = RunState.RUNTIME_PENDING
        elif observation.phase == AttemptPhase.RUNNING:
            record.state = RunState.RUNNING
        elif observation.phase == AttemptPhase.FAILED:
            self._fail_attempt(
                record,
                attempt,
                observation.error or "benchmark Job failed without a Kubernetes reason",
            )
        elif observation.phase == AttemptPhase.SUCCEEDED:
            try:
                self._promote_artifacts(record, attempt)
            except Exception as error:  # noqa: BLE001 - artifact failures are retryable.
                self._fail_attempt(
                    record, attempt, f"artifact promotion failed: {error}"
                )
            else:
                record.completed_concurrencies.append(attempt.concurrency)
                self._advance(record)
        record.updated_at = self.now()
        self.store.save(record)
        if record.state in TERMINAL_STATES:
            self._write_report(record)
        return record

    def reconcile_all(self) -> list[BenchmarkRecord]:
        return [
            self.reconcile(item.run_id)
            for item in self.store.list(limit=self.config.limits.maximum_list_results)
            if item.state not in TERMINAL_STATES
        ]

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        record = self.get(run_id)
        run_root = self._run_root(record.run_id)
        values: list[dict[str, Any]] = []
        if not run_root.exists():
            return values
        for path in sorted(run_root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(run_root).as_posix()
            values.append(
                {
                    "name": relative,
                    "size_bytes": path.stat().st_size,
                    "sha256": self._hash(path),
                }
            )
            if len(values) >= 500:
                break
        return values

    def get_report(self, run_id: str) -> dict[str, Any]:
        record = self.get(run_id)
        path = self._run_root(run_id) / "report.json"
        if not path.exists():
            self._write_report(record)
        return json.loads(path.read_text(encoding="utf-8"))

    def _start_attempt(
        self, record: BenchmarkRecord, concurrency: int, attempt_number: int
    ) -> None:
        root = self._run_root(record.run_id)
        attempt_directory = root / "attempts" / f"c{concurrency}" / f"a{attempt_number}"
        canonical = root / "canonical" / f"c{concurrency}"
        job_name = f"agentx-{record.run_id[:10]}-c{concurrency}-a{attempt_number}"
        attempt = AttemptRecord(
            concurrency=concurrency,
            attempt=attempt_number,
            job_name=job_name,
            submitted_at=self.now(),
            attempt_artifact_directory=str(attempt_directory),
            canonical_artifact_directory=str(canonical),
        )
        record.attempts.append(attempt)
        record.state = RunState.ADMISSION_PENDING
        record.updated_at = self.now()
        self.store.save(record)
        manifest = build_job_manifest(
            run_id=record.run_id,
            concurrency=concurrency,
            attempt=attempt_number,
            request=record.request,
            config=self.config,
            attempt_directory=str(attempt_directory),
        )
        queue = self.config.queues[record.request.local_queue]
        try:
            self.backend.create(queue.namespace, manifest)
        except Exception as error:  # noqa: BLE001 - backend failures become attempts.
            self._fail_attempt(record, attempt, f"Job submission failed: {error}")
            record.updated_at = self.now()
            self.store.save(record)

    def _fail_attempt(
        self, record: BenchmarkRecord, attempt: AttemptRecord, error: str
    ) -> None:
        attempt.phase = AttemptPhase.FAILED
        attempt.completed_at = attempt.completed_at or self.now()
        attempt.error = error[:2000]
        record.terminal_error = attempt.error
        attempts_for_concurrency = [
            item for item in record.attempts if item.concurrency == attempt.concurrency
        ]
        if len(attempts_for_concurrency) <= record.request.retries:
            self._start_attempt(
                record, attempt.concurrency, len(attempts_for_concurrency) + 1
            )
            return
        if attempt.concurrency not in record.failed_concurrencies:
            record.failed_concurrencies.append(attempt.concurrency)
        self._advance(record)

    def _advance(self, record: BenchmarkRecord) -> None:
        completed = set(record.completed_concurrencies) | set(
            record.failed_concurrencies
        )
        remaining = [
            value for value in record.request.concurrencies if value not in completed
        ]
        if remaining:
            record.active_concurrency_index = record.request.concurrencies.index(
                remaining[0]
            )
            self._start_attempt(record, remaining[0], 1)
            return
        if record.completed_concurrencies and record.failed_concurrencies:
            record.state = RunState.PARTIAL
            record.terminal_error = f"partial sweep: failed concurrency values {sorted(record.failed_concurrencies)}"
        elif record.failed_concurrencies:
            record.state = RunState.FAILED
            record.terminal_error = (
                f"all concurrency values failed: {sorted(record.failed_concurrencies)}"
            )
        else:
            record.state = RunState.COMPLETED
            record.terminal_error = None

    def _active_attempt(self, record: BenchmarkRecord) -> AttemptRecord | None:
        for attempt in reversed(record.attempts):
            if attempt.phase not in {
                AttemptPhase.SUCCEEDED,
                AttemptPhase.FAILED,
                AttemptPhase.CANCELED,
            }:
                return attempt
        return None

    def _run_root(self, run_id: str) -> Path:
        root = (
            Path(self.config.storage.mount_path)
            / self.config.storage.runs_subdirectory
            / run_id
        ).resolve()
        storage = Path(self.config.storage.mount_path).resolve()
        if storage not in root.parents:
            raise RuntimeError(
                "resolved artifact directory escaped the configured storage root"
            )
        return root

    def _promote_artifacts(
        self, record: BenchmarkRecord, attempt: AttemptRecord
    ) -> None:
        source = Path(attempt.attempt_artifact_directory).resolve()
        root = self._run_root(record.run_id)
        if root not in source.parents or not source.is_dir():
            raise RuntimeError(
                "attempt artifact directory is absent or outside the run root"
            )
        required = source / "profile_export_aiperf.json"
        if not required.is_file() or required.is_symlink():
            raise RuntimeError("profile_export_aiperf.json is missing")
        if required.stat().st_size > self.config.limits.maximum_profile_bytes:
            raise RuntimeError(
                "profile_export_aiperf.json exceeds the operator size limit"
            )
        if any(path.is_symlink() for path in source.rglob("*")):
            raise RuntimeError("artifact directories may not contain symbolic links")
        profile = json.loads(required.read_text(encoding="utf-8"))
        measurement_start = epoch_ns_to_iso(
            profile.get("min_request_timestamp", {}).get("avg")
        )
        measurement_end = epoch_ns_to_iso(
            profile.get("max_response_timestamp", {}).get("avg")
        )
        if measurement_start is None or measurement_end is None:
            raise RuntimeError(
                "AIPerf profile is missing exact request/response measurement timestamps"
            )
        destination = Path(attempt.canonical_artifact_directory).resolve()
        temporary = destination.with_name(
            f".{destination.name}.promoting-{attempt.attempt}"
        )
        if not destination.exists():
            if temporary.exists():
                shutil.rmtree(temporary)
            temporary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, temporary)
            os.replace(temporary, destination)
        attempt.artifact_hashes = {
            path.relative_to(destination).as_posix(): self._hash(path)
            for path in sorted(destination.rglob("*"))
            if path.is_file()
        }
        attempt.measurement_start = measurement_start
        attempt.measurement_end = measurement_end
        for filename, label in (
            ("server_metrics_export.json", "server metrics"),
            ("gpu_telemetry_export.jsonl", "GPU telemetry"),
        ):
            if not (destination / filename).is_file():
                attempt.monitoring_warnings.append(f"{label} artifact is missing")

    def _write_report(self, record: BenchmarkRecord) -> None:
        target = self.config.targets[record.request.logical_model_target]
        per_concurrency: list[dict[str, Any]] = []
        all_hashes: dict[str, str] = {}
        for concurrency in record.request.concurrencies:
            attempts = [
                item for item in record.attempts if item.concurrency == concurrency
            ]
            successful = next(
                (item for item in attempts if item.phase == AttemptPhase.SUCCEEDED),
                None,
            )
            metrics: dict[str, Any] = {}
            fingerprint = target.vllm_fingerprint
            if successful:
                canonical = Path(successful.canonical_artifact_directory)
                profile_path = canonical / "profile_export_aiperf.json"
                try:
                    profile = json.loads(profile_path.read_text(encoding="utf-8"))
                    metrics = {
                        key: self._bounded_metric(value)
                        for key, value in profile.items()
                        if any(
                            token in key.lower()
                            for token in (
                                "throughput",
                                "latency",
                                "time_to",
                                "inter_token",
                                "request_duration",
                            )
                        )
                        and self._bounded_metric(value) is not None
                    }
                except (OSError, json.JSONDecodeError):
                    pass
                fp_path = canonical / "vllm_fingerprint.txt"
                if fp_path.is_file():
                    with fp_path.open("r", encoding="utf-8") as stream:
                        fingerprint = stream.read(1024).strip()[:256] or fingerprint
                for name, digest in successful.artifact_hashes.items():
                    all_hashes[f"canonical/c{concurrency}/{name}"] = digest
            per_concurrency.append(
                {
                    "concurrency": concurrency,
                    "status": "succeeded"
                    if successful
                    else "failed"
                    if concurrency in record.failed_concurrencies
                    else "pending",
                    "attempts": [item.model_dump(mode="json") for item in attempts],
                    "interactivity_and_throughput": metrics,
                    "vllm_fingerprint": fingerprint,
                }
            )
        report = {
            "schema_version": 1,
            "run_id": record.run_id,
            "state": record.state,
            "scenario": {
                "name": record.request.scenario,
                **record.plan.scenario_validity,
            },
            "result_label": record.request.result_label,
            "vdp_workstream_id": record.vdp_workstream_id,
            "vllm": {
                "image": target.vllm_image,
                "fingerprint": target.vllm_fingerprint,
            },
            "measurement_windows": [
                {
                    "concurrency": item.concurrency,
                    "attempt": item.attempt,
                    "start": item.measurement_start,
                    "end": item.measurement_end,
                }
                for item in record.attempts
                if item.measurement_start or item.measurement_end
            ],
            "monitoring_provenance": {
                **record.plan.monitoring_sources,
                "server_artifacts": [
                    name for name in all_hashes if "server_metrics" in name
                ],
                "gpu_artifacts": [
                    name for name in all_hashes if "gpu_telemetry" in name
                ],
                "warnings": [
                    warning
                    for item in record.attempts
                    for warning in item.monitoring_warnings
                ],
            },
            "effective_configuration": {
                "request": record.request.model_dump(mode="json"),
                "target": record.plan.effective_target,
                "queue": record.plan.queue_resource_coverage,
                "aiperf_image": self.config.aiperf_image,
                "max_context_length": self.config.max_context_length,
            },
            "per_concurrency": per_concurrency,
            "artifact_hashes": all_hashes,
            "terminal_error": record.terminal_error,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
        path = self._run_root(record.run_id) / "report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _bounded_metric(value: Any) -> Any | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float, str)):
            return value
        if isinstance(value, dict):
            allowed = ("avg", "min", "p50", "p90", "p95", "p99", "max", "unit")
            result = {
                key: value[key]
                for key in allowed
                if isinstance(value.get(key), (int, float, str))
                and not isinstance(value.get(key), bool)
            }
            return result or None
        return None

    @staticmethod
    def _age_seconds(timestamp: str) -> float:
        parsed = datetime.fromisoformat(timestamp)
        return (datetime.now(UTC) - parsed).total_seconds()
