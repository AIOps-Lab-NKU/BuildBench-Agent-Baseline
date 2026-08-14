from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runner.validate_agent import ManifestError, parse_manifest, validate_manifest


VALID = """schema_version: "0.1"
agent:
  name: "example"
  version: "1.0.0"
runtime:
  type: "managed"
  profile: "python-3.11"
entrypoint:
  - "python"
  - "-m"
  - "src.main"
protocol:
  version: "0.1"
"""


class ValidateAgentTests(unittest.TestCase):
    def test_accepts_managed_python_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent.yaml"
            path.write_text(VALID, encoding="utf-8")
            entrypoint = validate_manifest(parse_manifest(path))
            self.assertEqual(entrypoint, ["python", "-m", "src.main"])

    def test_rejects_custom_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent.yaml"
            path.write_text(
                VALID.replace('type: "managed"', 'type: "custom"'),
                encoding="utf-8",
            )
            with self.assertRaises(ManifestError):
                validate_manifest(parse_manifest(path))

    def test_rejects_non_slug_agent_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent.yaml"
            path.write_text(
                VALID.replace('name: "example"', 'name: "Example Agent"'),
                encoding="utf-8",
            )
            with self.assertRaises(ManifestError):
                validate_manifest(parse_manifest(path))


if __name__ == "__main__":
    unittest.main()
