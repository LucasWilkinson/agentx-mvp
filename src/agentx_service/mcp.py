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
    ) -> tuple[int, dict[str, Any]]:
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
                    "supportedVersions": [PROTOCOL_VERSION],
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
            return 200, self._result(
                request_id,
                {
                    "tools": [tool.definition() for tool in self.tools],
                    "ttlMs": 300000,
                    "cacheScope": "private",
                },
            )
        if method != "tools/call":
            return self._error(request_id, -32601, f"Method not found: {method}", 404)
        arguments = params.get("arguments", {})
        tool = next((item for item in self.tools if item.name == body_name), None)
        if tool is None or not isinstance(arguments, dict):
            return self._error(
                request_id, -32602, f"Unknown or invalid tool: {body_name}", 400
            )
        try:
            value = tool.handler(arguments)
            payload = {
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
            value = {"error": str(error)}
            payload = {
                "content": [{"type": "text", "text": str(error)}],
                "structuredContent": value,
                "isError": True,
            }
        response = self._result(request_id, payload)
        encoded = json.dumps(response, default=str, separators=(",", ":")).encode()
        maximum = self.controller.config.limits.maximum_mcp_result_bytes
        if len(encoded) > maximum:
            response = self._result(
                request_id,
                {
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
                },
            )
        return 200, response

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
        return {"run_id": run_id, "artifacts": self.controller.list_artifacts(run_id)}

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
    def _error(request_id, code, message, status, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return status, {"jsonrpc": "2.0", "id": request_id, "error": error}
