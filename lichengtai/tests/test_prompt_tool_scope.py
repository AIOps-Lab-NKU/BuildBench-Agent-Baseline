from __future__ import annotations

import unittest
from pathlib import Path


class PromptToolScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prompt = (
            Path(__file__).resolve().parents[1]
            / "prompts"
            / "patch_generation.txt"
        ).read_text(encoding="utf-8")

    def test_build_validation_applies_to_every_package(self) -> None:
        self.assertIn(
            "Pass `{temp_dir}` to `run_build_validation_tool` "
            "for every package.",
            self.prompt,
        )

    def test_debian_preparation_scope_is_explicit(self) -> None:
        self.assertIn(
            "For Debian 3.0 (quilt), call "
            "`prepare_debian_source_tool",
            self.prompt,
        )

    def test_rpm_dependency_tool_is_not_for_debian(self) -> None:
        self.assertIn(
            "Use `query_dependency_api_tool` only for RPM/OBS Cases",
            self.prompt,
        )
        self.assertIn(
            "Do not call it for Debian source Cases.",
            self.prompt,
        )

    def test_old_unscoped_instruction_is_absent(self) -> None:
        self.assertNotIn(
            "Pass `{temp_dir}` to `prepare_debian_source_tool`, "
            "`query_dependency_api_tool`, and "
            "`run_build_validation_tool`.",
            self.prompt,
        )

    def test_prompt_contains_no_package_special_case(self) -> None:
        self.assertNotIn("pybdsf", self.prompt.lower())


if __name__ == "__main__":
    unittest.main()
