from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from .controller import BenchmarkController
from .models import BenchmarkRequest, RunId, RunState, StrictModel

PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = (PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION)
PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"


class RunReference(StrictModel):
    run_id: RunId


class ListRequest(StrictModel):
    state: RunState | None = None
    limit: int = Field(default=50, strict=True, ge=1, le=100)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    annotations: dict[str, bool]

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations,
        }


def _decoded_header(value: str | None) -> str | None:
    if value is None or not (value.startswith("=?base64?") and value.endswith("?=")):
        return value
    try:
        return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


class AgentXMcp:
    def __init__(
        self,
        controller: BenchmarkController,
        *,
        allowed_origins: set[str] | None = None,
    ) -> None:
        self.controller = controller
        self.allowed_origins = allowed_origins or set()
        read_only = {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        mutating = {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }
        cancel = {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        self.tools = sorted(
            [
                Tool(
                    "plan_agentx_benchmark",
                    "Validate and preview a bounded AgentX-MVP sweep without mutation.",
                    BenchmarkRequest.model_json_schema(),
                    self._plan,
                    read_only,
                ),
                Tool(
                    "submit_agentx_benchmark",
                    "Submit a planned AgentX-MVP sweep as Kueue-managed CPU Jobs.",
                    BenchmarkRequest.model_json_schema(),
                    self._submit,
                    mutating,
                ),
                Tool(
                    "list_agentx_benchmarks",
                    "List bounded benchmark summaries with an optional state filter.",
                    ListRequest.model_json_schema(),
                    self._list,
                    read_only,
                ),
                Tool(
                    "get_agentx_benchmark",
                    "Get lifecycle, admission, runtime, retry, and terminal status for one run.",
                    RunReference.model_json_schema(),
                    self._get,
                    read_only,
                ),
                Tool(
                    "cancel_agentx_benchmark",
                    "Cancel the active Job for a benchmark run; terminal runs are unchanged.",
                    RunReference.model_json_schema(),
                    self._cancel,
                    cancel,
                ),
                Tool(
                    "list_agentx_artifacts",
                    "List bounded artifact metadata and hashes; artifact contents are not returned.",
                    RunReference.model_json_schema(),
                    self._artifacts,
                    read_only,
                ),
                Tool(
                    "get_agentx_report",
                    "Get the bounded structured report for one AgentX benchmark run.",
                    RunReference.model_json_schema(),
                    self._report,
                    read_only,
                ),
            ],
            key=lambda item: item.name,
        )

    def handle(
        self, headers: dict[str, str], message: Any
    ) -> tuple[int, dict[str, Any] | None]:
        normalized = {key.lower(): value for key, value in headers.items()}
        origin = normalized.get("origin")
        if origin and origin not in self.allowed_origins:
            return self._error(None, -32600, "Origin is not allowed", 403)
        if (
            normalized.get("content-type", "").split(";", 1)[0].lower()
            != "application/json"
        ):
            return self._error(
                None, -32600, "Content-Type must be application/json", 415
            )
        if not isinstance(message, dict):
            return self._error(None, -32600, "Invalid Request", 400)
        request_id = message.get("id")
        params = message.get("params", {})
        method = message.get("method")
        requested_header = normalized.get("mcp-protocol-version")
        if method == "initialize" or requested_header == LEGACY_PROTOCOL_VERSION:
            return self._handle_legacy(message, params, request_id, method)
        if (
            message.get("jsonrpc") != "2.0"
            or isinstance(request_id, bool)
            or not isinstance(request_id, (str, int))
            or not isinstance(message.get("method"), str)
            or not isinstance(params, dict)
        ):
            return self._error(None, -32600, "Invalid Request", 400)
        metadata = params.get("_meta")
        client_info = (
            metadata.get(CLIENT_INFO_META) if isinstance(metadata, dict) else None
        )
        if (
            not isinstance(metadata, dict)
            or not isinstance(metadata.get(PROTOCOL_META), str)
            or not isinstance(metadata.get(CAPABILITIES_META), dict)
            or (
                client_info is not None
                and (
                    not isinstance(client_info, dict)
                    or not isinstance(client_info.get("name"), str)
                    or not isinstance(client_info.get("version"), str)
                )
            )
        ):
            return self._error(
                request_id,
                -32602,
                "Invalid params: required per-request MCP metadata is missing",
                400,
            )
        method = message["method"]
        requested = metadata[PROTOCOL_META]
        body_name = params.get("name") if method == "tools/call" else None
        if (
            normalized.get("mcp-protocol-version") != requested
            or normalized.get("mcp-method") != method
            or (
                method == "tools/call"
                and _decoded_header(normalized.get("mcp-name")) != body_name
            )
        ):
            return self._error(
                request_id,
                -32020,
                "Header mismatch: MCP request headers do not match the body",
                400,
            )
        if requested != PROTOCOL_VERSION:
            return self._error(
                request_id,
                -32022,
                "Unsupported protocol version",
                400,
                {"supported": [PROTOCOL_VERSION], "requested": requested},
            )
        if method == "server/discover":
            return 200, self._result(
                request_id,
                {
                    "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                    "capabilities": {"tools": {}},
                    "instructions": "Plan before submitting; inspect queue coverage and use reports rather than raw traces.",
                    "ttlMs": 300000,
                    "cacheScope": "private",
                },
            )
        if method == "tools/list":
            if params.get("cursor") is not None:
                return self._error(
                    request_id,
                    -32602,
                    "Invalid params: this tool list is not paginated",
                    400,
                )
            response = self._result(
                request_id,
                {
                    "tools": [tool.definition() for tool in self.tools],
                    "ttlMs": 300000,
                    "cacheScope": "private",
                },
            )
            return 200, self._bounded_response(request_id, response, legacy=False)
        if method != "tools/call":
            return self._error(request_id, -32601, f"Method not found: {method}", 404)
        arguments = params.get("arguments", {})
        tool = next((item for item in self.tools if item.name == body_name), None)
        if tool is None or not isinstance(arguments, dict):
            return self._error(
                request_id, -32602, f"Unknown or invalid tool: {body_name}", 400
            )
        payload = self._call_tool(tool, arguments)
        response = self._result(request_id, payload)
        return 200, self._bounded_response(request_id, response, legacy=False)

    def _handle_legacy(
        self,
        message: dict[str, Any],
        params: Any,
        request_id: Any,
        method: Any,
    ) -> tuple[int, dict[str, Any] | None]:
        """Serve the initialize-era, stateless MCP Streamable HTTP profile."""
        if (
            message.get("jsonrpc") != "2.0"
            or not isinstance(method, str)
            or not isinstance(params, dict)
        ):
            return self._error(None, -32600, "Invalid Request", 400)

        # JSON-RPC notifications intentionally have no response body. The service
        # has no subscriptions, cancellation side effects, or session state.
        if "id" not in message:
            return 202, None
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            return self._error(None, -32600, "Invalid Request", 400)

        if method == "initialize":
            requested = params.get("protocolVersion")
            capabilities = params.get("capabilities")
            client_info = params.get("clientInfo")
            if (
                not isinstance(requested, str)
                or not isinstance(capabilities, dict)
                or not isinstance(client_info, dict)
                or not isinstance(client_info.get("name"), str)
                or not isinstance(client_info.get("version"), str)
            ):
                return self._error(
                    request_id,
                    -32602,
                    "Invalid params: protocol version or client information is missing",
                    400,
                    {
                        "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
                        "requested": requested,
                    },
                )
            return 200, self._legacy_result(
                request_id,
                {
                    "protocolVersion": LEGACY_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "agentx-benchmark", "version": "0.1.0"},
                    "instructions": "Plan before submitting; inspect queue coverage and use reports rather than raw traces.",
                },
            )

        if method == "ping":
            return 200, self._legacy_result(request_id, {})
        if method == "tools/list":
            if params.get("cursor") is not None:
                return self._error(
                    request_id,
                    -32602,
                    "Invalid params: this tool list is not paginated",
                    200,
                )
            response = self._legacy_result(
                request_id, {"tools": [tool.definition() for tool in self.tools]}
            )
            return 200, self._bounded_response(request_id, response, legacy=True)
        if method != "tools/call":
            return self._error(request_id, -32601, f"Method not found: {method}", 200)

        name = params.get("name")
        arguments = params.get("arguments", {})
        tool = next((item for item in self.tools if item.name == name), None)
        if tool is None or not isinstance(arguments, dict):
            return self._error(
                request_id, -32602, f"Unknown or invalid tool: {name}", 200
            )
        payload = self._call_tool(tool, arguments)
        response = self._legacy_result(request_id, payload)
        return 200, self._bounded_response(request_id, response, legacy=True)

    def _call_tool(self, tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            value = tool.handler(arguments)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(value, default=str, separators=(",", ":")),
                    }
                ],
                "structuredContent": value,
                "isError": False,
            }
        except Exception as error:  # noqa: BLE001 - isolate tool failures.
            return {
                "content": [{"type": "text", "text": str(error)}],
                "structuredContent": {"error": str(error)},
                "isError": True,
            }

    def _bounded_response(
        self, request_id: str | int, response: dict[str, Any], *, legacy: bool
    ) -> dict[str, Any]:
        encoded = json.dumps(response, default=str, separators=(",", ":")).encode()
        maximum = self.controller.config.limits.maximum_mcp_result_bytes
        if len(encoded) <= maximum:
            return response
        payload = {
            "content": [
                {
                    "type": "text",
                    "text": "tool result exceeds the configured MCP response limit",
                }
            ],
            "structuredContent": {
                "size_bytes": len(encoded),
                "maximum_bytes": maximum,
            },
            "isError": True,
        }
        if legacy:
            return self._legacy_result(request_id, payload)
        return self._result(request_id, payload)

    @staticmethod
    def _validated(model, arguments):
        return model.model_validate(arguments)

    def _plan(self, arguments):
        return self.controller.plan(
            self._validated(BenchmarkRequest, arguments)
        ).model_dump(mode="json")

    def _submit(self, arguments):
        return self.controller.submit(
            self._validated(BenchmarkRequest, arguments)
        ).model_dump(mode="json")

    def _list(self, arguments):
        values = self._validated(ListRequest, arguments)
        return {
            "benchmarks": [
                {
                    "run_id": item.run_id,
                    "state": item.state,
                    "result_label": item.request.result_label,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "terminal_error": item.terminal_error,
                }
                for item in self.controller.list(
                    state=values.state.value if values.state else None,
                    limit=values.limit,
                )
            ]
        }

    def _get(self, arguments):
        return self.controller.get(
            self._validated(RunReference, arguments).run_id
        ).model_dump(mode="json")

    def _cancel(self, arguments):
        return self.controller.cancel(
            self._validated(RunReference, arguments).run_id
        ).model_dump(mode="json")

    def _artifacts(self, arguments):
        run_id = self._validated(RunReference, arguments).run_id
        return {"run_id": run_id, **self.controller.list_artifacts(run_id)}

    def _report(self, arguments):
        return self.controller.get_report(
            self._validated(RunReference, arguments).run_id
        )

    @staticmethod
    def _result(request_id, result):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "resultType": "complete",
                **result,
                "_meta": {
                    SERVER_INFO_META: {"name": "agentx-benchmark", "version": "0.1.0"}
                },
            },
        }

    @staticmethod
    def _legacy_result(request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id, code, message, status, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return status, {"jsonrpc": "2.0", "id": request_id, "error": error}
