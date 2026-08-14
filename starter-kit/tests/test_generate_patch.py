from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runner.generate_patch import PatchGenerationError, generate_patch


class GeneratePatchTests(unittest.TestCase):
    def test_generates_validator_compatible_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original"
            modified = root / "modified"
            output = root / "repair.diff"
            (original / "input").mkdir(parents=True)
            (modified / "input").mkdir(parents=True)
            (original / "input" / "package.spec").write_text(
                "old\n", encoding="utf-8"
            )
            (modified / "input" / "package.spec").write_text(
                "new\n", encoding="utf-8"
            )

            changed = generate_patch(original, modified, output, "input/")

            self.assertEqual(changed, ("input/package.spec",))
            patch = output.read_text(encoding="utf-8")
            self.assertIn(
                "diff --git a/input/package.spec b/input/package.spec", patch
            )
            self.assertIn("--- a/input/package.spec", patch)
            self.assertIn("+++ b/input/package.spec", patch)

    def test_rejects_change_outside_allowed_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original"
            modified = root / "modified"
            original.mkdir()
            modified.mkdir()
            (original / "manifest.json").write_text("{}\n", encoding="utf-8")
            (modified / "manifest.json").write_text(
                '{"changed": true}\n', encoding="utf-8"
            )
            with self.assertRaises(PatchGenerationError):
                generate_patch(
                    original,
                    modified,
                    root / "repair.diff",
                    "input/",
                )

    def test_ignores_unchanged_binary_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original"
            modified = root / "modified"
            output = root / "repair.diff"
            for case in (original, modified):
                (case / "input").mkdir(parents=True)
                (case / "dependencies").mkdir()
                (case / "dependencies" / "package.rpm").write_bytes(
                    b"\x00unchanged-rpm"
                )
            (original / "input" / "package.spec").write_text(
                "old\n", encoding="utf-8"
            )
            (modified / "input" / "package.spec").write_text(
                "new\n", encoding="utf-8"
            )

            changed = generate_patch(original, modified, output, "input/")

            self.assertEqual(changed, ("input/package.spec",))


if __name__ == "__main__":
    unittest.main()
