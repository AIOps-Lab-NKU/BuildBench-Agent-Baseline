"""Load build-backend configuration without exposing provider credentials."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tools.runtime_config import (
    CONFIG_ENVIRONMENT_VARIABLE,
    DEFAULT_CONFIG_PATH,
    apply_run_state_isolation,
    load_agent_config,
)


def load_build_config(
    config_path: Path | None = None,
) -> dict[str, Any]:
    selected_path = config_path

    if (
        selected_path is None
        and not os.getenv(
            CONFIG_ENVIRONMENT_VARIABLE,
            "",
        ).strip()
    ):
        selected_path = DEFAULT_CONFIG_PATH

    root = load_agent_config(selected_path)

    build = dict(root.get("build") or {})
    configured = str(
        build.get("backend", "obs")
    ).strip().lower()

    build["backend"] = os.getenv(
        "BUILD_BENCH_BACKEND",
        configured,
    ).strip().lower()

    docker = dict(build.get("docker") or {})

    environment_overrides = {
        "validator_command": (
            "BUILD_BENCH_VALIDATOR_COMMAND"
        ),
        "case_store_dir": "BUILD_BENCH_CASE_STORE",
        "result_root": "BUILD_BENCH_RESULT_ROOT",
        "staging_root": "BUILD_BENCH_STAGING_ROOT",
        "worker_mode": "BUILD_BENCH_WORKER_MODE",
    }

    for field, variable in environment_overrides.items():
        if os.getenv(variable):
            docker[field] = os.environ[variable]

    build["docker"] = docker

    # BB_RUN_ROOT is the final authority for mutable
    # output locations, even when legacy path override
    # variables are also present.
    isolated = apply_run_state_isolation(
        {"build": build}
    )

    return dict(
        isolated.get("build") or {}
    )
