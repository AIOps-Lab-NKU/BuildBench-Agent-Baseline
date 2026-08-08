"""Protocol shared by every Build-Bench build backend."""

from __future__ import annotations

from typing import Protocol

from .models import BuildRequest, BuildResult


class BuildBackend(Protocol):
    name: str

    def build(self, request: BuildRequest) -> BuildResult:
        """Build the current Agent workspace and return a normalized result."""
