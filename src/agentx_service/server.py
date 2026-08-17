from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .backend import KubectlBackend
from .config import load_operator_config
from .controller import BenchmarkController
from .mcp import AgentXMcp


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    config = load_operator_config()
    controller = BenchmarkController(config, KubectlBackend())
    controller.reconstruct()
    allowed = {
        value
        for value in os.environ.get("AGENTX_ALLOWED_ORIGINS", "").split(",")
        if value
    }
    mcp = AgentXMcp(controller, allowed_origins=allowed)
    lock = threading.RLock()
    stopped = threading.Event()

    def reconcile_loop() -> None:
        interval = max(2, int(os.environ.get("AGENTX_RECONCILE_SECONDS", "10")))
        while not stopped.wait(interval):
            with lock:
                controller.reconcile_all()

    threading.Thread(
        target=reconcile_loop, name="agentx-reconciler", daemon=True
    ).start()

    class Handler(BaseHTTPRequestHandler):
        server_version = "agentx-service/0.1"

        def do_GET(self) -> None:
            if self.path in {"/healthz", "/readyz"}:
                self._json(200, {"status": "ok"})
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
            with lock:
                status, response = mcp.handle(dict(self.headers.items()), message)
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
