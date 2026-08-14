from __future__ import annotations

import tempfile
import unittest
import zipfile
import shutil
from pathlib import Path

from runner.agent_submission import (
    SubmissionError,
    check_submission,
    create_deterministic_zip,
    initialize_agent,
)


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "managed-python-agent"
EXAMPLE = ROOT / "agents" / "example-agent"


class AgentSubmissionTests(unittest.TestCase):
    def test_example_agent_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "example-agent"
            shutil.copytree(
                EXAMPLE,
                copied,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            report = check_submission(copied)
            self.assertEqual(report.agent_name, "buildbench-example-agent")
            self.assertEqual(report.entrypoint, ("python", "-m", "src.main"))

    def test_initialize_agent_replaces_name_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = initialize_agent(TEMPLATE, root / "my-agent", "my-agent")
            self.assertIn(
                'name: "my-agent"',
                (target / "agent.yaml").read_text(encoding="utf-8"),
            )
            check_submission(target)
            with self.assertRaises(SubmissionError):
                initialize_agent(TEMPLATE, target, "my-agent")

    def test_rejects_generated_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = initialize_agent(
                TEMPLATE,
                Path(temporary) / "my-agent",
                "my-agent",
            )
            (target / "repair.diff").write_text("generated", encoding="utf-8")
            with self.assertRaisesRegex(SubmissionError, "unexpected top-level"):
                check_submission(target)

    def test_rejects_unpinned_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = initialize_agent(
                TEMPLATE,
                Path(temporary) / "my-agent",
                "my-agent",
            )
            (target / "requirements.lock").write_text(
                "requests>=2\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SubmissionError, "must pin"):
                check_submission(target)

    def test_rejects_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = initialize_agent(
                TEMPLATE,
                Path(temporary) / "my-agent",
                "my-agent",
            )
            (target / "src" / "secret.py").write_text(
                'TOKEN = "sk-abcdefghijklmnopqrstuvwxyz123456"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SubmissionError, "credential"):
                check_submission(target)

    def test_package_is_deterministic_and_has_only_contract_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = initialize_agent(TEMPLATE, root / "my-agent", "my-agent")
            first = root / "first.zip"
            second = root / "second.zip"
            first_digest, _ = create_deterministic_zip(target, first)
            second_digest, _ = create_deterministic_zip(target, second)
            self.assertEqual(first_digest, second_digest)
            with zipfile.ZipFile(first) as archive:
                names = set(archive.namelist())
            self.assertIn("agent.yaml", names)
            self.assertIn("src/main.py", names)
            self.assertIn("requirements.lock", names)
            self.assertIn("README.md", names)
            self.assertFalse(any(name.startswith("runs/") for name in names))


if __name__ == "__main__":
    unittest.main()
