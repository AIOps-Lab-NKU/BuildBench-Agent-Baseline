from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


LAUNCHER_PATH = (
    Path(__file__).resolve().parents[1]
    / "automation"
    / "parallel_agent_launcher.py"
)

SPEC = importlib.util.spec_from_file_location(
    "parallel_agent_launcher",
    LAUNCHER_PATH,
)

assert SPEC is not None
assert SPEC.loader is not None

launcher = importlib.util.module_from_spec(
    SPEC
)

sys.modules[SPEC.name] = launcher
SPEC.loader.exec_module(launcher)


class ParallelAutomationLauncherTests(
    unittest.TestCase
):
    def setUp(self):
        self.temp = (
            tempfile.TemporaryDirectory()
        )

        self.root = Path(
            self.temp.name
        ).resolve()

        self.repo = self.root / "repo"
        self.repo.mkdir()

        (
            self.repo
            / "client_patch.py"
        ).write_text(
            "print('not executed')\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def make_config(
        self,
        name: str,
        case_name: str,
    ) -> Path:
        base_dir = (
            self.root
            / f"{name}-base"
        )

        (
            base_dir
            / case_name
        ).mkdir(parents=True)

        config_path = (
            self.root
            / f"{name}.yaml"
        )

        config_path.write_text(
            yaml.safe_dump(
                {
                    "LLM_PROVIDER": "qwen",
                    "paths": {
                        "base_dir": str(
                            base_dir
                        ),
                        "result_dir": (
                            "/legacy/results"
                        ),
                        "log_dir": (
                            "/legacy/logs"
                        ),
                        "temp_work_dir": (
                            "/legacy/work"
                        ),
                    },
                    "build": {
                        "backend": "docker",
                        "docker": {
                            "validator_command": (
                                "validator"
                            ),
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        return config_path

    def make_plan(
        self,
        jobs: list[dict],
        max_parallel: int = 2,
    ) -> Path:
        path = self.root / "plan.yaml"

        path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "0.1",
                    "max_parallel": (
                        max_parallel
                    ),
                    "client_script": str(
                        self.repo
                        / "client_patch.py"
                    ),
                    "jobs": jobs,
                }
            ),
            encoding="utf-8",
        )

        return path

    def load(self, path: Path):
        return launcher.load_plan(
            path,
            personal_base=self.root,
            repo_root=self.repo,
        )

    def test_two_disjoint_jobs_validate(
        self,
    ):
        first = self.make_config(
            "first",
            "case-a",
        )

        second = self.make_config(
            "second",
            "case-b",
        )

        plan = self.make_plan(
            [
                {
                    "job_id": "a",
                    "config_path": str(
                        first
                    ),
                    "run_root": str(
                        self.root / "run-a"
                    ),
                },
                {
                    "job_id": "b",
                    "config_path": str(
                        second
                    ),
                    "run_root": str(
                        self.root / "run-b"
                    ),
                },
            ]
        )

        loaded = self.load(plan)

        self.assertEqual(
            loaded.max_parallel,
            2,
        )

        self.assertEqual(
            len(loaded.jobs),
            2,
        )

    def test_parallel_limit_above_two_is_rejected(
        self,
    ):
        config = self.make_config(
            "first",
            "case-a",
        )

        plan = self.make_plan(
            [
                {
                    "job_id": "a",
                    "config_path": str(
                        config
                    ),
                    "run_root": str(
                        self.root / "run-a"
                    ),
                }
            ],
            max_parallel=3,
        )

        with self.assertRaises(
            RuntimeError
        ):
            self.load(plan)

    def test_duplicate_run_root_is_rejected(
        self,
    ):
        first = self.make_config(
            "first",
            "case-a",
        )

        second = self.make_config(
            "second",
            "case-b",
        )

        shared = (
            self.root
            / "shared-run"
        )

        plan = self.make_plan(
            [
                {
                    "job_id": "a",
                    "config_path": str(
                        first
                    ),
                    "run_root": str(
                        shared
                    ),
                },
                {
                    "job_id": "b",
                    "config_path": str(
                        second
                    ),
                    "run_root": str(
                        shared
                    ),
                },
            ]
        )

        with self.assertRaises(
            RuntimeError
        ):
            self.load(plan)

    def test_multi_case_base_dir_is_rejected(
        self,
    ):
        config = self.make_config(
            "first",
            "case-a",
        )

        data = yaml.safe_load(
            config.read_text(
                encoding="utf-8"
            )
        )

        base_dir = Path(
            data["paths"]["base_dir"]
        )

        (
            base_dir
            / "case-b"
        ).mkdir()

        plan = self.make_plan(
            [
                {
                    "job_id": "a",
                    "config_path": str(
                        config
                    ),
                    "run_root": str(
                        self.root / "run-a"
                    ),
                }
            ]
        )

        with self.assertRaises(
            RuntimeError
        ):
            self.load(plan)

    def test_job_environment_is_isolated(
        self,
    ):
        config = self.make_config(
            "first",
            "case-a",
        )

        plan = self.make_plan(
            [
                {
                    "job_id": "a",
                    "config_path": str(
                        config
                    ),
                    "run_root": str(
                        self.root / "run-a"
                    ),
                    "model": "qwen-test",
                }
            ]
        )

        job = self.load(plan).jobs[0]

        environment = (
            launcher.build_job_environment(
                job,
                {
                    "BB_WORKSPACE": (
                        "/shared/output"
                    ),
                    "BUILD_BENCH_RESULT_ROOT": (
                        "/legacy/results"
                    ),
                    "BUILD_BENCH_STAGING_ROOT": (
                        "/legacy/staging"
                    ),
                },
            )
        )

        self.assertNotIn(
            "BB_WORKSPACE",
            environment,
        )

        self.assertNotIn(
            "BUILD_BENCH_RESULT_ROOT",
            environment,
        )

        self.assertNotIn(
            "BUILD_BENCH_STAGING_ROOT",
            environment,
        )

        self.assertEqual(
            environment[
                "BB_AGENT_CONFIG"
            ],
            str(config),
        )

        self.assertEqual(
            environment["BB_RUN_ROOT"],
            str(self.root / "run-a"),
        )

        self.assertEqual(
            environment["LLM_MODEL"],
            "qwen-test",
        )

    def test_historical_prep_only_run_root_is_allowed(
        self,
    ):
        run_root = self.root / "run-01"
        (run_root / "prep").mkdir(
            parents=True
        )

        launcher._assert_run_root_ready(
            run_root
        )

    def test_existing_runtime_output_is_rejected(
        self,
    ):
        run_root = self.root / "run-01"
        run_root.mkdir()

        (
            run_root
            / "launcher-console.log"
        ).write_text(
            "old run\n",
            encoding="utf-8",
        )

        with self.assertRaises(
            RuntimeError
        ):
            launcher._assert_run_root_ready(
                run_root
            )

    def test_prep_file_instead_of_directory_is_rejected(
        self,
    ):
        run_root = self.root / "run-01"
        run_root.mkdir()

        (run_root / "prep").write_text(
            "not a directory\n",
            encoding="utf-8",
        )

        with self.assertRaises(
            RuntimeError
        ):
            launcher._assert_run_root_ready(
                run_root
            )

    def test_semaphore_never_exceeds_two(
        self,
    ):
        configs = [
            self.make_config(
                f"job-{index}",
                f"case-{index}",
            )
            for index in range(4)
        ]

        plan_path = self.make_plan(
            [
                {
                    "job_id": (
                        f"job-{index}"
                    ),
                    "config_path": str(
                        config
                    ),
                    "run_root": str(
                        self.root
                        / f"run-{index}"
                    ),
                }
                for index, config
                in enumerate(configs)
            ]
        )

        plan = self.load(plan_path)

        active = 0
        peak = 0

        async def fake_runner(job):
            nonlocal active
            nonlocal peak

            active += 1
            peak = max(
                peak,
                active,
            )

            await asyncio.sleep(0.01)

            active -= 1

            return {
                "job_id": job.job_id,
                "state": "completed",
            }

        results = asyncio.run(
            launcher.execute_plan(
                plan,
                runner=fake_runner,
            )
        )

        self.assertEqual(
            len(results),
            4,
        )

        self.assertEqual(
            peak,
            2,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
