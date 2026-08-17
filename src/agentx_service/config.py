from __future__ import annotations

import json
import os
from pathlib import Path

from .models import OperatorConfig


def load_operator_config(path: str | Path | None = None) -> OperatorConfig:
    """Load strict operator-owned JSON. Request payloads never override it."""
    selected = Path(
        path
        or os.environ.get("AGENTX_OPERATOR_CONFIG", "/etc/agentx/operator-config.json")
    )
    try:
        value = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"cannot load operator config {selected}: {error}"
        ) from error
    return OperatorConfig.model_validate(value)
