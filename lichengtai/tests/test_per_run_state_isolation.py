from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import yaml

from tools.build_backends.config import (
    load_build_config,
)
from tools.runtime_config import (
    RUN_ROOT_ENVIRONMENT_VARIABLE,
    apply_run_state_isolation,
    load_agent_config,
    resolve_run_root,
)


@contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)

    try:
        yield
    finally:
        os.chdir(previous)


class PerRunStateIsolationTests(unittest.TestCase):
    def write_config(self, path: Path) -> None:
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        payload = {
            "LLM_PROVIDER": "qwen",
            "paths": {
                "base_dir": "/canonical/base",
                "result_dir": "/legacy/results",
                "log_dir": "/legacy/logs",
                "temp_work_dir": "/legacy/workspaces",
            },
            "build": {
                "backend": "docker",
                "client_tool_timeout_seconds": 7200,
                "docker": {
                    "validator_command": "validator",
                    "case_store_dir": "/canonical/cases",
                    "result_root": "/legacy/docker-results",
                    "staging_root": "/legacy/staging",
                    "input_guard_state_dir": (
                        "/legacy/input-guard"
                    ),
                },
            },
        }

        path.write_text(
            yaml.safe_dump(
                payload,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def assert_isolated_paths(
        self,
        config: dict,
        run_root: Path,
    ) -> None:
        paths = config["paths"]
        docker = config["build"]["docker"]

        self.assertEqual(
            paths["result_dir"],
            str(run_root / "agent-results"),
        )
        self.assertEqual(
            paths["log_dir"],
            str(run_root / "agent-logs"),
        )
        self.assertEqual(
            paths["temp_work_dir"],
            str(run_root / "agent-workspaces"),
        )
        self.assertEqual(
            docker["result_root"],
            str(run_root / "docker-results"),
        )
        self.assertEqual(
            docker["staging_root"],
            str(run_root / "docker-staging"),
        )
        self.assertEqual(
            docker["input_guard_state_dir"],
            str(run_root / "input-guard"),
        )

    def test_run_root_overrides_mutable_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_path = root / "info.yaml"
            run_root = (root / "run-a").resolve()

            self.write_config(config_path)

            with patch.dict(
                os.environ,
                {
                    RUN_ROOT_ENVIRONMENT_VARIABLE: (
                        str(run_root)
                    )
                },
                clear=False,
            ):
                loaded = load_agent_config(config_path)

            self.assert_isolated_paths(
                loaded,
                run_root,
            )

            self.assertEqual(
                loaded["paths"]["base_dir"],
                "/canonical/base",
            )
            self.assertEqual(
                loaded["build"]["docker"][
                    "case_store_dir"
                ],
                "/canonical/cases",
            )

    def test_no_run_root_preserves_old_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "info.yaml"
            self.write_config(config_path)

            with patch.dict(
                os.environ,
                {},
                clear=False,
            ):
                os.environ.pop(
                    RUN_ROOT_ENVIRONMENT_VARIABLE,
                    None,
                )
                loaded = load_agent_config(config_path)

            self.assertEqual(
                loaded["paths"]["result_dir"],
                "/legacy/results",
            )
            self.assertEqual(
                loaded["build"]["docker"][
                    "result_root"
                ],
                "/legacy/docker-results",
            )

    def test_two_run_roots_are_disjoint(self):
        base_config = {
            "paths": {
                "base_dir": "/canonical/base",
            },
            "build": {
                "backend": "docker",
                "docker": {
                    "validator_command": "validator",
                },
            },
        }

        first_root = Path("/tmp/run-a")
        second_root = Path("/tmp/run-b")

        first = apply_run_state_isolation(
            base_config,
            first_root,
        )
        second = apply_run_state_isolation(
            base_config,
            second_root,
        )

        first_paths = {
            first["paths"]["result_dir"],
            first["paths"]["log_dir"],
            first["paths"]["temp_work_dir"],
            first["build"]["docker"]["result_root"],
            first["build"]["docker"]["staging_root"],
            first["build"]["docker"][
                "input_guard_state_dir"
            ],
        }

        second_paths = {
            second["paths"]["result_dir"],
            second["paths"]["log_dir"],
            second["paths"]["temp_work_dir"],
            second["build"]["docker"]["result_root"],
            second["build"]["docker"]["staging_root"],
            second["build"]["docker"][
                "input_guard_state_dir"
            ],
        }

        self.assertTrue(
            first_paths.isdisjoint(second_paths)
        )

    def test_backend_run_root_wins_over_legacy_overrides(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_path = root / "info.yaml"
            run_root = (root / "isolated-run").resolve()

            self.write_config(config_path)

            environment = {
                RUN_ROOT_ENVIRONMENT_VARIABLE: (
                    str(run_root)
                ),
                "BUILD_BENCH_RESULT_ROOT": (
                    "/legacy/environment/results"
                ),
                "BUILD_BENCH_STAGING_ROOT": (
                    "/legacy/environment/staging"
                ),
            }

            with patch.dict(
                os.environ,
                environment,
                clear=False,
            ):
                loaded = load_build_config(config_path)

            docker = loaded["docker"]

            self.assertEqual(
                docker["result_root"],
                str(run_root / "docker-results"),
            )
            self.assertEqual(
                docker["staging_root"],
                str(run_root / "docker-staging"),
            )
            self.assertEqual(
                docker["input_guard_state_dir"],
                str(run_root / "input-guard"),
            )

    def test_relative_run_root_uses_current_directory(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw:
            current = Path(raw)

            with working_directory(current):
                with patch.dict(
                    os.environ,
                    {
                        RUN_ROOT_ENVIRONMENT_VARIABLE: (
                            "runs/case-a"
                        )
                    },
                    clear=False,
                ):
                    resolved = resolve_run_root()

            self.assertEqual(
                resolved,
                (current / "runs/case-a").resolve(),
            )

    def test_filesystem_root_is_rejected(self):
        filesystem_root = Path(
            Path.cwd().anchor
        )

        with self.assertRaises(RuntimeError):
            resolve_run_root(filesystem_root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
