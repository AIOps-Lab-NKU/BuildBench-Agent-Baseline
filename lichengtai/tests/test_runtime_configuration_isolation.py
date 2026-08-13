from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import yaml

from tools.build_backends.config import load_build_config
from tools.runtime_config import (
    CONFIG_ENVIRONMENT_VARIABLE,
    load_agent_config,
    resolve_agent_config_path,
)


@contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class RuntimeConfigurationIsolationTests(
    unittest.TestCase
):
    def write_config(
        self,
        path: Path,
        *,
        provider: str,
        backend: str = "docker",
    ) -> None:
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        payload = {
            "LLM_PROVIDER": provider,
            "paths": {
                "base_dir": "/tmp/base",
                "result_dir": "/tmp/results",
                "log_dir": "/tmp/logs",
                "temp_work_dir": "/tmp/work",
            },
            "build": {
                "backend": backend,
                "client_tool_timeout_seconds": 7200,
                "docker": {
                    "validator_command": "validator",
                    "result_root": "/tmp/build-results",
                    "staging_root": "/tmp/staging",
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

    def test_environment_config_has_precedence(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            default = root / "config/info.yaml"
            selected = root / "runs/a/info.yaml"

            self.write_config(
                default,
                provider="openai",
            )
            self.write_config(
                selected,
                provider="qwen",
            )

            with working_directory(root):
                with patch.dict(
                    os.environ,
                    {
                        CONFIG_ENVIRONMENT_VARIABLE: (
                            str(selected)
                        )
                    },
                    clear=False,
                ):
                    loaded = load_agent_config()

            self.assertEqual(
                loaded["LLM_PROVIDER"],
                "qwen",
            )

    def test_default_config_remains_compatible(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            default = root / "config/info.yaml"

            self.write_config(
                default,
                provider="qwen",
            )

            with working_directory(root):
                with patch.dict(
                    os.environ,
                    {},
                    clear=False,
                ):
                    os.environ.pop(
                        CONFIG_ENVIRONMENT_VARIABLE,
                        None,
                    )
                    loaded = load_agent_config()

            self.assertEqual(
                loaded["LLM_PROVIDER"],
                "qwen",
            )

    def test_explicit_path_beats_environment(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            explicit = root / "explicit.yaml"
            environment = root / "environment.yaml"

            self.write_config(
                explicit,
                provider="qwen",
            )
            self.write_config(
                environment,
                provider="openai",
            )

            with patch.dict(
                os.environ,
                {
                    CONFIG_ENVIRONMENT_VARIABLE: (
                        str(environment)
                    )
                },
                clear=False,
            ):
                loaded = load_agent_config(explicit)

            self.assertEqual(
                loaded["LLM_PROVIDER"],
                "qwen",
            )

    def test_relative_environment_path_is_resolved(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            selected = root / "runs/a/info.yaml"

            self.write_config(
                selected,
                provider="qwen",
            )

            with working_directory(root):
                with patch.dict(
                    os.environ,
                    {
                        CONFIG_ENVIRONMENT_VARIABLE: (
                            "runs/a/info.yaml"
                        )
                    },
                    clear=False,
                ):
                    resolved = (
                        resolve_agent_config_path()
                    )

            self.assertEqual(
                resolved,
                selected.resolve(),
            )

    def test_backend_reads_same_environment_config(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw:
            selected = Path(raw) / "run-info.yaml"

            self.write_config(
                selected,
                provider="qwen",
                backend="docker",
            )

            with patch.dict(
                os.environ,
                {
                    CONFIG_ENVIRONMENT_VARIABLE: (
                        str(selected)
                    )
                },
                clear=False,
            ):
                loaded = load_build_config()

            self.assertEqual(
                loaded["backend"],
                "docker",
            )
            self.assertEqual(
                loaded["docker"]["result_root"],
                "/tmp/build-results",
            )

    def test_client_has_no_fixed_global_info(
        self,
    ):
        source = Path(
            "client_patch.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn(
            'with open("config/info.yaml"',
            source,
        )
        self.assertNotIn(
            'provider = info["LLM_PROVIDER"]',
            source,
        )
        self.assertIn(
            "config_path = "
            "resolve_agent_config_path()",
            source,
        )
        self.assertIn(
            'os.environ["BB_AGENT_CONFIG"]',
            source,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
