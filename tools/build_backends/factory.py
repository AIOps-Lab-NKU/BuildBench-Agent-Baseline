"""Factory for the configured Build-Bench build backend."""

from __future__ import annotations

from .base import BuildBackend
from .config import load_build_config
from .docker import DockerBuildBackend
from .obs import ObsBuildBackend


def create_build_backend() -> BuildBackend:
    config = load_build_config()
    backend = config.get("backend", "obs")
    if backend == "obs":
        return ObsBuildBackend()
    if backend == "docker":
        return DockerBuildBackend(dict(config.get("docker") or {}))
    raise RuntimeError(f"Unsupported build backend: {backend!r}")
