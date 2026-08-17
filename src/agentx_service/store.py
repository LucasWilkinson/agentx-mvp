from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from .models import BenchmarkRecord


class FileRunStore:
    """Atomic PVC-backed state; one JSON document per durable run."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.state = self.root / "state"

    def _path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{24}", run_id):
            raise ValueError("invalid run id")
        return self.state / f"{run_id}.json"

    def save(self, record: BenchmarkRecord) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        destination = self._path(record.run_id)
        payload = record.model_dump_json(indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=f".{record.run_id}.", dir=self.state)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def get(self, run_id: str) -> BenchmarkRecord | None:
        path = self._path(run_id)
        try:
            return BenchmarkRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def list(
        self, *, state: str | None = None, limit: int = 100
    ) -> list[BenchmarkRecord]:
        records: list[BenchmarkRecord] = []
        for path in self.state.glob("*.json"):
            try:
                record = BenchmarkRecord.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            if state is None or record.state.value == state:
                records.append(record)
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records[:limit]

    def check_writable(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=".readiness-", dir=self.state)
        os.close(fd)
        os.unlink(path)

    def delete(self, run_id: str) -> None:
        path = self._path(run_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        run_root = self.root / run_id
        if run_root.is_dir() and run_root.parent == self.root:
            shutil.rmtree(run_root)
