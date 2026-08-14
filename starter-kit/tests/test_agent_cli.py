from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_bb(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROOT / "bb"), *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class AgentCliContractTests(unittest.TestCase):
    def test_bootstrap_rejects_invalid_name_as_json(self) -> None:
        result = run_bb("bootstrap", "Invalid_Name", "--json")
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["command"], "bootstrap")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["exit_code"], 2)
        self.assertTrue(payload["diagnostics"])

    def test_bootstrap_refuses_existing_agent_without_overwrite(self) -> None:
        target = ROOT / "agents" / "test-existing-contract"
        marker = target / "keep.txt"
        target.mkdir(parents=True, exist_ok=False)
        marker.write_text("preserve\n", encoding="utf-8")
        try:
            result = run_bb("bootstrap", target.name, "--json")
            self.assertEqual(result.returncode, 4)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["exit_code"], 4)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve\n")
        finally:
            shutil.rmtree(target)

    def test_ready_reports_missing_agent_as_json(self) -> None:
        result = run_bb(
            "ready",
            "--agent",
            "./agents/does-not-exist",
            "--json",
        )
        self.assertEqual(result.returncode, 4)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["command"], "ready")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["exit_code"], 4)
        self.assertEqual(payload["artifacts"], [])

    def test_dispatcher_reports_release_candidate_version(self) -> None:
        result = run_bb("version")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "0.1.0-rc.2")


if __name__ == "__main__":
    unittest.main()
