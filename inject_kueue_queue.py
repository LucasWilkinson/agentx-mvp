#!/usr/bin/env python3
"""Add a Kueue queue label to rendered manifesto LeaderWorkerSets."""

from __future__ import annotations

import argparse
import sys
from typing import Any

import yaml


QUEUE_LABEL = "kueue.x-k8s.io/queue-name"


def add_queue_label(obj: dict[str, Any], queue: str) -> None:
    if obj.get("kind") != "LeaderWorkerSet":
        return
    metadata = obj.setdefault("metadata", {})
    labels = metadata.setdefault("labels", {})
    labels[QUEUE_LABEL] = queue


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True, help="Kueue LocalQueue name")
    args = parser.parse_args()

    docs = list(yaml.safe_load_all(sys.stdin))
    for doc in docs:
        if isinstance(doc, dict):
            add_queue_label(doc, args.queue)

    yaml.safe_dump_all(
        docs,
        sys.stdout,
        explicit_start=True,
        sort_keys=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
