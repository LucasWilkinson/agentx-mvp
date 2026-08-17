from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .backend import KubectlBackend
from .config import load_operator_config
from .controller import TERMINAL_STATES, BenchmarkController
from .models import BenchmarkRequest
from .planner import plan_benchmark
from .server import serve


def _request(path: str) -> BenchmarkRequest:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return BenchmarkRequest.model_validate_json(text)


def main() -> int:
    parser = argparse.ArgumentParser(prog="agentx-service")
    parser.add_argument("--config", help="operator-owned JSON config")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "submit", "run"):
        item = sub.add_parser(name)
        item.add_argument("request", help="strict request JSON file, or - for stdin")
    get = sub.add_parser("get")
    get.add_argument("run_id")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("run_id")
    sub.add_parser("reconcile")
    server = sub.add_parser("serve")
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if args.command == "serve":
        if args.config:
            __import__("os").environ["AGENTX_OPERATOR_CONFIG"] = args.config
        serve(args.host, args.port)
        return 0
    config = load_operator_config(args.config)
    if args.command == "plan":
        value = plan_benchmark(_request(args.request), config)
        print(value.model_dump_json(indent=2))
        return 0
    controller = BenchmarkController(config, KubectlBackend())
    if args.command == "submit":
        value = controller.submit(_request(args.request))
    elif args.command == "get":
        value = controller.get(args.run_id)
    elif args.command == "cancel":
        value = controller.cancel(args.run_id)
    elif args.command == "reconcile":
        value = controller.reconcile_all()
    else:
        value = controller.submit(_request(args.request))
        while value.state not in TERMINAL_STATES:
            time.sleep(10)
            value = controller.reconcile(value.run_id)
    if hasattr(value, "model_dump_json"):
        print(value.model_dump_json(indent=2))
    else:
        print(
            json.dumps(
                [item.model_dump(mode="json") for item in value], indent=2, default=str
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
