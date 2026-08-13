"""Tests for Docker Validator artifact-contract classification."""

from __future__ import annotations

import unittest

from tools.build_backends.docker import _classify_validator_result


class DockerArtifactContractClassificationTests(unittest.TestCase):
    def test_debian_artifact_pattern_mismatch_is_classified(self) -> None:
        raw = {
            "status": "failed",
            "artifact_validation_passed": False,
            "build_exit_code": 0,
            "message": (
                "build failed; expected binary artifact pattern "
                "'pybdsf_*_arm64.deb' was not matched"
            ),
            "artifacts": [
                {
                    "kind": "binary",
                    "path": (
                        "artifacts/"
                        "python3-bdsf_1.9.2-3build4_arm64.deb"
                    ),
                },
                {
                    "kind": "source",
                    "path": "artifacts/pybdsf_1.9.2-3build4.dsc",
                },
            ],
        }

        status, success, message = _classify_validator_result(raw)

        self.assertEqual(status, "artifact_contract_mismatch")
        self.assertFalse(success)
        self.assertIn("package build completed", message.lower())
        self.assertIn("do not modify source code", message.lower())

    def test_real_build_failure_is_not_reclassified(self) -> None:
        raw = {
            "status": "failed",
            "artifact_validation_passed": False,
            "build_exit_code": 1,
            "message": "package build failed",
            "artifacts": [
                {
                    "kind": "source",
                    "path": "artifacts/example_1.0-1.dsc",
                }
            ],
        }

        status, success, message = _classify_validator_result(raw)

        self.assertEqual(status, "failed")
        self.assertFalse(success)
        self.assertEqual(message, "package build failed")

    def test_normal_verified_success_is_unchanged(self) -> None:
        raw = {
            "status": "succeeded",
            "artifact_validation_passed": True,
            "build_exit_code": 0,
            "message": "build completed successfully",
            "artifacts": [
                {
                    "kind": "binary",
                    "path": "artifacts/example_1.0-1_arm64.deb",
                }
            ],
        }

        status, success, message = _classify_validator_result(raw)

        self.assertEqual(status, "succeeded")
        self.assertTrue(success)
        self.assertEqual(message, "build completed successfully")


if __name__ == "__main__":
    unittest.main()
