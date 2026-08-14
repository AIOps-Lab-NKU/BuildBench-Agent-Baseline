from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import sys
from pathlib import Path

sys.dont_write_bytecode = True


AGENT_MAIN = (
    Path(__file__).parents[1]
    / "agents"
    / "example-agent"
    / "src"
    / "main.py"
)
SPEC = importlib.util.spec_from_file_location("example_agent_main", AGENT_MAIN)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ExampleAgentTests(unittest.TestCase):
    def test_repairs_only_demo_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "input").mkdir()
            (workspace / "work" / "repo" / "input").mkdir(parents=True)
            (workspace / "input" / "initial-build.log").write_text(
                "intentional Build-Bench demo failure\n",
                encoding="utf-8",
            )
            spec = workspace / "work" / "repo" / "input" / "buildbench-hello.spec"
            spec.write_text(MODULE.BROKEN_BLOCK, encoding="utf-8")

            result = MODULE.repair(workspace)

            self.assertEqual(result["status"], "completed")
            self.assertNotIn(MODULE.BROKEN_BLOCK, spec.read_text(encoding="utf-8"))
            self.assertIn("BUILD-BENCH-DEMO-REPAIRED", spec.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
