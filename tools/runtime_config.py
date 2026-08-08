"""Shared runtime-configuration loading for Client and build backends."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


CONFIG_ENVIRONMENT_VARIABLE = "BB_AGENT_CONFIG"
RUN_ROOT_ENVIRONMENT_VARIABLE = "BB_RUN_ROOT"
DEFAULT_CONFIG_PATH = Path("config/info.yaml")


def resolve_agent_config_path(
    config_path: str | Path | None = None,
) -> Path:
    """Resolve one configuration path for the current Agent process."""

    selected: str | Path

    if config_path is not None:
        selected = config_path
    else:
        environment_value = os.getenv(
            CONFIG_ENVIRONMENT_VARIABLE,
            "",
        ).strip()

        selected = (
            environment_value
            if environment_value
            else DEFAULT_CONFIG_PATH
        )

    path = Path(selected).expanduser()

    if not path.is_absolute():
        path = Path.cwd() / path

    return path.resolve()


def resolve_run_root(
    run_root: str | Path | None = None,
) -> Path | None:
    """Resolve the optional root for one isolated Agent run."""

    selected: str | Path | None = run_root

    if selected is None:
        environment_value = os.getenv(
            RUN_ROOT_ENVIRONMENT_VARIABLE,
            "",
        ).strip()

        selected = (
            environment_value
            if environment_value
            else None
        )

    if selected is None:
        return None

    path = Path(selected).expanduser()

    if not path.is_absolute():
        path = Path.cwd() / path

    path = path.resolve()

    if path == Path(path.anchor):
        raise RuntimeError(
            "BB_RUN_ROOT cannot be a filesystem root"
        )

    if path.exists() and not path.is_dir():
        raise RuntimeError(
            f"BB_RUN_ROOT is not a directory: {path}"
        )

    return path


def apply_run_state_isolation(
    config: dict[str, Any],
    run_root: str | Path | None = None,
) -> dict[str, Any]:
    """Derive all mutable output paths from one optional run root."""

    isolated = dict(config)
    root = resolve_run_root(run_root)

    if root is None:
        return isolated

    paths = dict(isolated.get("paths") or {})

    paths.update(
        {
            "result_dir": str(root / "agent-results"),
            "log_dir": str(root / "agent-logs"),
            "temp_work_dir": str(
                root / "agent-workspaces"
            ),
        }
    )

    isolated["paths"] = paths

    build = dict(isolated.get("build") or {})
    docker = dict(build.get("docker") or {})

    docker.update(
        {
            "result_root": str(
                root / "docker-results"
            ),
            "staging_root": str(
                root / "docker-staging"
            ),
            "input_guard_state_dir": str(
                root / "input-guard"
            ),
        }
    )

    build["docker"] = docker
    isolated["build"] = build

    return isolated


def load_agent_config(
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load the non-secret Agent configuration mapping."""

    path = resolve_agent_config_path(config_path)

    try:
        with path.open("r", encoding="utf-8") as stream:
            root = yaml.safe_load(stream) or {}
    except OSError as error:
        raise RuntimeError(
            f"Build-Bench config could not be read: {path}"
        ) from error
    except yaml.YAMLError as error:
        raise RuntimeError(
            f"Build-Bench config is invalid YAML: {path}"
        ) from error

    if not isinstance(root, dict):
        raise RuntimeError(
            f"Build-Bench config must be a mapping: {path}"
        )

    return apply_run_state_isolation(
        dict(root)
    )
