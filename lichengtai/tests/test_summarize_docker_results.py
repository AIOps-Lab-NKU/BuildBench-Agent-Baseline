#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "summarize_docker_results.py"
)


class SummarizeDockerResultsTests(unittest.TestCase):
    def run_summary(self, results: list[dict]) -> str:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            for index, result in enumerate(results, 1):
                result_dir = root / f"run-{index:02d}"
                result_dir.mkdir()
                (result_dir / "build-result.json").write_text(
                    json.dumps(result),
                    encoding="utf-8",
                )

            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(root)],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                completed.returncode,
                0,
                msg=completed.stderr,
            )
            return completed.stdout

    def test_failed_build_is_not_success(self) -> None:
        output = self.run_summary(
            [
                {
                    "status": "failed",
                    "artifact_validation_passed": False,
                    "build_exit_code": 1,
                    "artifacts": [],
                }
            ]
        )

        self.assertIn(
            "verified_success_count=0",
            output,
        )
        self.assertIn(
            "final_verdict=no_verified_success",
            output,
        )

    def test_succeeded_build_with_valid_artifacts_is_success(self) -> None:
        output = self.run_summary(
            [
                {
                    "status": "failed",
                    "artifact_validation_passed": False,
                    "build_exit_code": 1,
                    "artifacts": [],
                },
                {
                    "status": "succeeded",
                    "artifact_validation_passed": True,
                    "build_exit_code": 0,
                    "duration_seconds": 120,
                    "artifacts": [
                        {"path": "artifacts/example.rpm"},
                    ],
                },
            ]
        )

        self.assertIn(
            "verified_success_count=1",
            output,
        )
        self.assertIn(
            "final_verdict=verified_build_success",
            output,
        )

    def test_succeeded_status_without_artifact_validation_is_not_success(
        self,
    ) -> None:
        output = self.run_summary(
            [
                {
                    "status": "succeeded",
                    "artifact_validation_passed": False,
                    "build_exit_code": 0,
                    "artifacts": [],
                }
            ]
        )

        self.assertIn(
            "verified_success_count=0",
            output,
        )
        self.assertIn(
            "final_verdict=no_verified_success",
            output,
        )


if __name__ == "__main__":
    unittest.main()
