"""Thin service boundary shared by both MCP servers."""

from __future__ import annotations

from pathlib import Path

from .factory import create_build_backend
from .models import BuildRequest, BuildResult


def run_build_validation(package_path: str) -> BuildResult:
    workspace = Path(package_path)
    if not workspace.is_dir():
        return BuildResult(
            backend="unknown",
            status="infrastructure_error",
            success=False,
            message=f"Package workspace is not a directory: {package_path}",
            case_id=workspace.name or "unknown",
        )

    try:
        backend = create_build_backend()
        return backend.build(
            BuildRequest(package_name=workspace.name, workspace=workspace)
        )
    except Exception as error:
        return BuildResult(
            backend="unknown",
            status="infrastructure_error",
            success=False,
            message=f"Build backend initialization failed: {error}",
            case_id=workspace.name,
        )
