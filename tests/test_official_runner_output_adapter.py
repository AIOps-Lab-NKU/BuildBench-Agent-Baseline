from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from client_patch import AutoRepairClientPatch


class OfficialRunnerOutputAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_workspace = os.environ.get("BB_WORKSPACE")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        if self.old_workspace is None:
            os.environ.pop("BB_WORKSPACE", None)
        else:
            os.environ["BB_WORKSPACE"] = self.old_workspace
        self.temp.cleanup()

    def make_client(
        self,
        *,
        outcome: str = "success",
        stop_reason: str = "build_succeeded",
    ) -> AutoRepairClientPatch:
        client = object.__new__(AutoRepairClientPatch)
        client.result_dir = str(self.root / "internal-results")
        client.active_experiment = {
            "case_id": "example-case",
            "final_status": outcome,
            "stop_reason": stop_reason,
            "modified_paths": [
                "src/example.c",
                "src/example.c",
                "",
            ],
        }
        client._log = lambda *args, **kwargs: None
        return client

    def experiment_result(self) -> Path:
        path = self.root / "internal-results" / "experiment-result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        return path

    def test_internal_result_remains_available(self) -> None:
        os.environ.pop("BB_WORKSPACE", None)
        client = self.make_client()

        path = client._write_agent_result(
            "example-case",
            str(self.experiment_result()),
        )

        self.assertIsNotNone(path)
        payload = json.loads(
            Path(path).read_text(encoding="utf-8")
        )
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["outcome"], "success")
        self.assertFalse((self.root / "output").exists())

    def test_bb_workspace_receives_identical_result(self) -> None:
        workspace = self.root / "official-workspace"
        os.environ["BB_WORKSPACE"] = str(workspace)

        client = self.make_client()
        internal_path = Path(
            client._write_agent_result(
                "example-case",
                str(self.experiment_result()),
            )
        )
        official_path = workspace / "output" / "agent-result.json"

        self.assertTrue(internal_path.is_file())
        self.assertTrue(official_path.is_file())

        internal = json.loads(
            internal_path.read_text(encoding="utf-8")
        )
        official = json.loads(
            official_path.read_text(encoding="utf-8")
        )
        self.assertEqual(official, internal)

    def test_mismatch_is_mirrored_as_completed_outcome(self) -> None:
        workspace = self.root / "official-workspace"
        os.environ["BB_WORKSPACE"] = str(workspace)

        client = self.make_client(
            outcome="artifact_contract_mismatch",
            stop_reason="artifact_contract_mismatch",
        )
        client._write_agent_result(
            "example-case",
            str(self.experiment_result()),
        )

        payload = json.loads(
            (
                workspace
                / "output"
                / "agent-result.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(
            payload["outcome"],
            "artifact_contract_mismatch",
        )
        self.assertEqual(
            payload["stop_reason"],
            "artifact_contract_mismatch",
        )

    def test_existing_official_result_is_replaced(self) -> None:
        workspace = self.root / "official-workspace"
        official_path = workspace / "output" / "agent-result.json"
        official_path.parent.mkdir(parents=True)
        official_path.write_text(
            '{"status":"stale"}\n',
            encoding="utf-8",
        )
        os.environ["BB_WORKSPACE"] = str(workspace)

        client = self.make_client()
        client._write_agent_result(
            "example-case",
            str(self.experiment_result()),
        )

        payload = json.loads(
            official_path.read_text(encoding="utf-8")
        )
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["case_id"], "example-case")

    def test_bad_workspace_does_not_destroy_internal_result(
        self,
    ) -> None:
        workspace_file = self.root / "not-a-directory"
        workspace_file.write_text("file\n", encoding="utf-8")
        os.environ["BB_WORKSPACE"] = str(workspace_file)

        messages = []
        client = self.make_client()
        client._log = lambda *args: messages.append(args)

        internal_path = client._write_agent_result(
            "example-case",
            str(self.experiment_result()),
        )

        self.assertTrue(Path(internal_path).is_file())
        self.assertTrue(
            any(
                "Official Agent result mirror failed"
                in str(item)
                for message in messages
                for item in message
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
