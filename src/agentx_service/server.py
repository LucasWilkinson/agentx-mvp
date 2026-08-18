from __future__ import annotations

import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .backend import KubectlBackend
from .config import load_operator_config
from .controller import BenchmarkController
from .mcp import AgentXMcp
from .monitoring import PrometheusMonitoring


def authorized(authorization: str | None, token: str) -> bool:
    return hmac.compare_digest(authorization or "", f"Bearer {token}")


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    config = load_operator_config()
    api_token = os.environ.get("AGENTX_API_TOKEN")
    if not api_token:
        raise RuntimeError("AGENTX_API_TOKEN is required")
    controller = BenchmarkController(
        config, KubectlBackend(), monitoring=PrometheusMonitoring()
    )
    controller.reconstruct()
    allowed = {
        value
        for value in os.environ.get("AGENTX_ALLOWED_ORIGINS", "").split(",")
        if value
    }
    mcp = AgentXMcp(controller, allowed_origins=allowed)
    stopped = threading.Event()
    reconciliation = {"healthy": True, "error": None}
    readiness = {"healthy": False, "detail": {}, "error": "not checked yet"}
    readiness_lock = threading.Lock()

    def reconcile_loop() -> None:
        interval = max(2, int(os.environ.get("AGENTX_RECONCILE_SECONDS", "10")))
        while not stopped.wait(interval):
            try:
                controller.reconcile_all()
                reconciliation.update(healthy=True, error=None)
            except Exception as error:  # noqa: BLE001 - keep controller alive.
                reconciliation.update(healthy=False, error=str(error)[:1000])

    def readiness_loop() -> None:
        while not stopped.is_set():
            try:
                detail = controller.readiness()
            except Exception as error:  # noqa: BLE001 - readiness fails closed.
                value = {"healthy": False, "detail": {}, "error": str(error)[:1000]}
            else:
                value = {"healthy": True, "detail": detail, "error": None}
            with readiness_lock:
                readiness.update(value)
            if stopped.wait(10):
                break

    for target, name in (
        (reconcile_loop, "agentx-reconciler"),
        (readiness_loop, "agentx-readiness"),
    ):
        threading.Thread(target=target, name=name, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        server_version = "agentx-service/0.1"

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._json(200, {"status": "ok"})
            elif self.path == "/readyz":
                with readiness_lock:
                    cached = dict(readiness)
                if not cached["healthy"] or not reconciliation["healthy"]:
                    error = cached["error"] or (
                        f"reconciler unhealthy: {reconciliation['error']}"
                    )
                    self._json(503, {"status": "not-ready", "error": error})
                else:
                    self._json(200, {"status": "ready", **cached["detail"]})
            else:
                self._json(
                    405 if self.path == "/mcp" else 404,
                    {
                        "error": "method not allowed"
                        if self.path == "/mcp"
                        else "not found"
                    },
                )

        def do_POST(self) -> None:
            if self.path != "/mcp":
                self._json(404, {"error": "not found"})
                return
            if not authorized(self.headers.get("authorization"), api_token):
                self._json(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("content-length", "0"))
            except ValueError:
                length = 0
            maximum = config.limits.maximum_mcp_result_bytes
            if length <= 0 or length > maximum:
                self._json(
                    413,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32600,
                            "message": "request body is empty or exceeds the configured limit",
                        },
                    },
                )
                return
            try:
                message = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json(
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    },
                )
                return
            status, response = mcp.handle(dict(self.headers.items()), message)
            if response is None:
                self.send_response(status)
                self.send_header("content-length", "0")
                self.send_header("cache-control", "no-store")
                self.end_headers()
            else:
                self._json(status, response)

        def _json(self, status: int, value) -> None:
            payload = json.dumps(value, default=str, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args) -> None:
            print(f"agentx-service: {fmt % args}", flush=True)

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        stopped.set()
        server.server_close()
