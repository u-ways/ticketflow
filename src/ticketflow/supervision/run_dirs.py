"""Per-attempt run directories (ADR-0010).

runs/<node_id>/<attempt>/
    meta.json    pid, start_time, runner, model, session_id
    prompt.md    the dispatched prompt (for the record and the spawn)
    stdout.log / stderr.log
    exit_code    existence = completion signal; contents = result
    heartbeat    mtime = last sign of life
"""

import gzip
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RunDir:
    path: Path

    @property
    def meta_path(self) -> Path:
        return self.path / "meta.json"

    @property
    def prompt_path(self) -> Path:
        return self.path / "prompt.md"

    @property
    def stdout_path(self) -> Path:
        return self.path / "stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self.path / "stderr.log"

    @property
    def exit_code_path(self) -> Path:
        return self.path / "exit_code"

    @property
    def heartbeat_path(self) -> Path:
        return self.path / "heartbeat"

    def create(self) -> RunDir:
        self.path.mkdir(parents=True, exist_ok=True)
        return self

    def write_meta(self, meta: dict[str, Any]) -> None:
        self.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def read_meta(self) -> dict[str, Any]:
        raw: dict[str, Any] = json.loads(self.meta_path.read_text(encoding="utf-8"))
        return raw

    def read_exit_code(self) -> int | None:
        """None while running; the file's existence is the completion signal."""
        if not self.exit_code_path.is_file():
            return None
        text = self.exit_code_path.read_text(encoding="utf-8").strip()
        return int(text) if text else None

    def heartbeat_age_seconds(self, now: datetime) -> float | None:
        if not self.heartbeat_path.is_file():
            return None
        mtime = datetime.fromtimestamp(self.heartbeat_path.stat().st_mtime, tz=UTC)
        return (now.astimezone(UTC) - mtime).total_seconds()

    def compress_logs(self) -> list[Path]:
        """Gzip logs on attempt completion (ADR-0010). Returns compressed files."""
        compressed = []
        for log in (self.stdout_path, self.stderr_path):
            if log.is_file():
                target = log.with_suffix(log.suffix + ".gz")
                with log.open("rb") as src, gzip.open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                log.unlink()
                compressed.append(target)
        return compressed
