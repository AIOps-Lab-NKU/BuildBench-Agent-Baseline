from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AgentGuidanceTests(unittest.TestCase):
    def test_agents_guide_documents_safe_autonomous_path(self) -> None:
        guide = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("./bb bootstrap my-agent --json", guide)
        self.assertIn("./bb ready --agent ./agents/my-agent --json", guide)
        self.assertIn("coding assistant", guide)
        self.assertIn("Repair Agent", guide)
        self.assertIn("dist/agent-submission.zip", guide)
        self.assertNotIn("10.10.1.226", guide)
        self.assertNotIn("/home/zhaochenyu", guide)

    def test_compatibility_guide_has_no_duplicate_contract(self) -> None:
        self.assertEqual(
            (ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip(),
            "@AGENTS.md",
        )

    def test_public_dispatcher_exposes_high_level_commands(self) -> None:
        dispatcher = (ROOT / "bb").read_text(encoding="utf-8")
        self.assertIn("bootstrap)", dispatcher)
        self.assertIn("ready)", dispatcher)


if __name__ == "__main__":
    unittest.main()
