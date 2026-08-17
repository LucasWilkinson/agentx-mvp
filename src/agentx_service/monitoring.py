from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import MonitoringConfig, TargetConfig, utc_now


class MonitoringBackend(Protocol):
    def preflight(
        self, target: TargetConfig, profile: MonitoringConfig
    ) -> dict[str, Any]: ...

    def capture(
        self,
        profile: MonitoringConfig,
        start: str,
        end: str,
        destination: Path,
    ) -> dict[str, Any]: ...

    def ready(self, profile: MonitoringConfig) -> dict[str, Any]: ...

    def warmup(
        self, target: TargetConfig, served_model_name: str
    ) -> dict[str, Any]: ...


class PrometheusMonitoring:
    """Bounded health/uniqueness gates and exact-window Prometheus export."""

    def __init__(self, *, maximum_response_bytes: int = 8_388_608) -> None:
        self.maximum_response_bytes = maximum_response_bytes

    def _json(self, url: str, *, method: str = "GET") -> dict[str, Any]:
        request = Request(url, method=method, headers={"accept": "application/json"})
        with urlopen(request, timeout=15) as response:
            body = response.read(self.maximum_response_bytes + 1)
        if len(body) > self.maximum_response_bytes:
            raise RuntimeError(
                f"monitoring response exceeded {self.maximum_response_bytes} bytes"
            )
        value = json.loads(body)
        if not isinstance(value, dict):
            raise TypeError("monitoring endpoint did not return a JSON object")
        return value

    def _text(self, url: str) -> str:
        request = Request(url, method="GET", headers={"accept": "text/plain"})
        with urlopen(request, timeout=15) as response:
            body = response.read(4097)
        if len(body) > 4096:
            raise RuntimeError("monitoring readiness response is too large")
        return body.decode("utf-8", errors="replace")

    def _post_json(self, url: str, value: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(value).encode()
        request = Request(
            url,
            data=body,
            method="POST",
            headers={"content-type": "application/json", "accept": "application/json"},
        )
        with urlopen(request, timeout=600) as response:
            result = response.read(self.maximum_response_bytes + 1)
        if len(result) > self.maximum_response_bytes:
            raise RuntimeError("warmup response exceeded the configured limit")
        value = json.loads(result)
        if not isinstance(value, dict):
            raise TypeError("warmup endpoint did not return a JSON object")
        return value

    def _query(self, profile: MonitoringConfig, query: str) -> dict[str, Any]:
        url = f"{profile.prometheus_url}/api/v1/query?{urlencode({'query': query})}"
        value = self._json(url)
        if value.get("status") != "success":
            raise RuntimeError(
                f"Prometheus query failed: {value.get('error', 'unknown error')}"
            )
        return value

    @staticmethod
    def _validate_targets(
        value: dict[str, Any], expected: int, label: str
    ) -> dict[str, Any]:
        results = value.get("data", {}).get("result", [])
        if not isinstance(results, list):
            raise TypeError(f"{label} target query returned an invalid result")
        identities: list[tuple[str, str]] = []
        unidentified: list[int] = []
        unhealthy: list[str] = []
        for index, item in enumerate(results):
            metric = item.get("metric", {})
            identity = (
                str(metric.get("pod", "")),
                str(metric.get("endpoint", metric.get("instance", ""))),
            )
            identities.append(identity)
            if not all(identity):
                unidentified.append(index)
            sample = item.get("value", [None, "0"])
            if not isinstance(sample, list) or len(sample) < 2 or str(sample[1]) != "1":
                unhealthy.append("/".join(identity))
        duplicates = sorted({item for item in identities if identities.count(item) > 1})
        if len(results) != expected or unhealthy or duplicates or unidentified:
            raise RuntimeError(
                f"{label} targets invalid: expected={expected} observed={len(results)} "
                f"unhealthy={unhealthy} duplicates={duplicates} "
                f"unidentified_indexes={unidentified}"
            )
        return {
            "expected": expected,
            "observed": len(results),
            "identities": identities,
        }

    def preflight(
        self, target: TargetConfig, profile: MonitoringConfig
    ) -> dict[str, Any]:
        models = self._json(f"{target.endpoint_url}/models")
        served = {
            str(item.get("id"))
            for item in models.get("data", [])
            if isinstance(item, dict) and item.get("id")
        }
        if not served.intersection(target.served_model_names):
            raise RuntimeError(
                "target health check did not advertise an allowed served model"
            )
        server = self._validate_targets(
            self._query(profile, profile.server_up_query),
            profile.expected_server_targets,
            "server",
        )
        gpu = self._validate_targets(
            self._query(profile, profile.gpu_up_query),
            profile.expected_gpu_targets,
            "GPU",
        )
        runtime = self._json(f"{profile.prometheus_url}/api/v1/status/runtimeinfo")
        grafana = self._json(f"{profile.grafana_url}/api/health")
        return {
            "checked_at": utc_now(),
            "target_models": sorted(served),
            "server_targets": server,
            "gpu_targets": gpu,
            "prometheus_runtime": runtime.get("data", {}),
            "grafana": grafana,
        }

    def capture(
        self,
        profile: MonitoringConfig,
        start: str,
        end: str,
        destination: Path,
    ) -> dict[str, Any]:
        exports: dict[str, Any] = {}
        for name, query in profile.capture_queries.items():
            params = urlencode(
                {"query": query, "start": start, "end": end, "step": "15s"}
            )
            value = self._json(f"{profile.prometheus_url}/api/v1/query_range?{params}")
            if value.get("status") != "success":
                raise RuntimeError(f"Prometheus range query {name} failed")
            exports[name] = {"query": query, "response": value}
        payload = {
            "captured_at": utc_now(),
            "exact_window": {"start": start, "end": end},
            "prometheus_url": profile.prometheus_url,
            "grafana_url": profile.grafana_url,
            "queries": exports,
        }
        (destination / "prometheus_export.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        return {
            "captured_at": payload["captured_at"],
            "exact_window": payload["exact_window"],
            "prometheus_url": profile.prometheus_url,
            "grafana_url": profile.grafana_url,
            "query_names": sorted(exports),
            "artifact": "prometheus_export.json",
        }

    def ready(self, profile: MonitoringConfig) -> dict[str, Any]:
        value = self._text(f"{profile.prometheus_url}/-/ready")
        return {"prometheus": "ok", "response": value}

    def warmup(self, target: TargetConfig, served_model_name: str) -> dict[str, Any]:
        started = utc_now()
        value = self._post_json(
            f"{target.endpoint_url}/chat/completions",
            {
                "model": served_model_name,
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 8,
            },
        )
        fingerprint = value.get("system_fingerprint")
        return {
            "started_at": started,
            "completed_at": utc_now(),
            "system_fingerprint": fingerprint,
            "response_id": value.get("id"),
        }
