from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from client_patch import AutoRepairClientPatch


class AgentResultContractTests(unittest.TestCase):
    def make_client(
        self,
        root: Path,
        modified_paths: list[str] | None = None,
    ) -> AutoRepairClientPatch:
        client = object.__new__(AutoRepairClientPatch)
        client.result_dir = str(root)
        client.log_dir = str(root / "logs")
        client.active_experiment = {
            "case_id": "example-case",
            "final_status": "running",
            "stop_reason": None,
            "modified_paths": list(modified_paths or []),
        }

        client._active_attempt = lambda package_name: None
        client._save_experiment = lambda package_name: str(
            root / "example-case_result_experiment-result.json"
        )
        client._log = lambda tag, message: None

        return client

    def load_result(self, root: Path) -> dict:
        path = (
            root
            / "example-case"
            / "agent-result.json"
        )

        self.assertTrue(path.is_file(), path)

        return json.loads(
            path.read_text(encoding="utf-8")
        )

    def test_success_writes_completed_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = self.make_client(
                root,
                [
                    "src/example.c",
                    "debian/control",
                    "src/example.c",
                ],
            )

            client._finish_experiment(
                "example-case",
                "success",
                "build_succeeded",
            )

            result = self.load_result(root)

            self.assertEqual(result["schema_version"], "0.1")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["outcome"], "success")
            self.assertEqual(
                result["stop_reason"],
                "build_succeeded",
            )
            self.assertEqual(
                result["modified_paths"],
                [
                    "debian/control",
                    "src/example.c",
                ],
            )
            self.assertEqual(
                result["experiment_result"],
                "example-case_result_experiment-result.json",
            )
            self.assertEqual(
                result["generated_by"],
                "client-framework",
            )

    def test_terminal_build_failure_is_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = self.make_client(root)

            client._finish_experiment(
                "example-case",
                "build_failed",
                "max_build_attempts",
            )

            result = self.load_result(root)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                result["outcome"],
                "build_failed",
            )
            self.assertEqual(
                result["stop_reason"],
                "max_build_attempts",
            )

    def test_client_error_is_not_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = self.make_client(root)

            client._finish_experiment(
                "example-case",
                "client_error",
                "RuntimeError: synthetic failure",
                "attempt_1",
            )

            result = self.load_result(root)

            self.assertEqual(result["status"], "error")
            self.assertEqual(
                result["outcome"],
                "client_error",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
