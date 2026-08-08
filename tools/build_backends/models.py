"""Backend-neutral build result model."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BuildRequest:
    package_name: str
    workspace: Path


@dataclass(frozen=True)
class BuildResult:
    backend: str
    status: str
    success: bool
    message: str
    case_id: str
    target_arch: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    duration_seconds: float | None = None
    log_path: str | None = None
    result_path: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    outcome_evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
