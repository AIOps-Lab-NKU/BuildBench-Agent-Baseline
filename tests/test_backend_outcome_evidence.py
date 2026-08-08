from __future__ import annotations

import json
import unittest

from server_patch import _parse_build_result_payload
from tools.build_backends.docker import (
    _classify_validator_result,
    _validator_outcome_evidence,
)
from tools.build_backends.models import BuildResult


class BackendOutcomeEvidenceTests(unittest.TestCase):
    def mismatch_raw(self) -> dict:
        return {
            "case_id": "example-riscv64-case",
            "status": "failed",
            "artifact_validation_passed": False,
            "build_exit_code": 0,
            "target_arch": "riscv64",
            "message": (
                "build failed; expected binary artifact pattern "
                "'example_*_riscv64.deb' was not matched"
            ),
            "artifacts": [
                {
                    "kind": "binary",
                    "path": (
                        "artifacts/"
                        "python3-example_1.0-1_riscv64.deb"
                    ),
                },
                {
                    "kind": "binary",
                    "path": (
                        "artifacts/"
                        "python3-example-dbgsym_"
                        "1.0-1_riscv64.ddeb"
                    ),
                },
                {
                    "kind": "source",
                    "path": "artifacts/example_1.0-1.dsc",
                },
            ],
        }

    def test_debian_mismatch_produces_generic_evidence(
        self,
    ) -> None:
        raw = self.mismatch_raw()

        status, success, message = (
            _classify_validator_result(raw)
        )
        evidence = _validator_outcome_evidence(
            raw,
            status=status,
            success=success,
        )

        self.assertEqual(
            status,
            "artifact_contract_mismatch",
        )
        self.assertFalse(success)

        self.assertEqual(
            evidence["kind"],
            "artifact_contract_mismatch",
        )
        self.assertTrue(
            evidence["underlying_build_succeeded"]
        )
        self.assertFalse(
            evidence["artifact_validation_passed"]
        )
        self.assertEqual(
            evidence["produced_binary_artifact_count"],
            2,
        )
        self.assertEqual(
            evidence["package_format"],
            "deb",
        )
        self.assertEqual(
            evidence["target_arch"],
            "riscv64",
        )
        self.assertEqual(
            evidence["evidence_source"],
            "validator_result",
        )
        self.assertEqual(
            evidence["classifier"],
            "docker-debian-artifact-contract-v1",
        )

    def test_real_build_failure_has_no_mismatch_evidence(
        self,
    ) -> None:
        raw = {
            "status": "failed",
            "artifact_validation_passed": False,
            "build_exit_code": 1,
            "target_arch": "riscv64",
            "message": "compiler exited with status 1",
            "artifacts": [],
        }

        status, success, _ = (
            _classify_validator_result(raw)
        )
        evidence = _validator_outcome_evidence(
            raw,
            status=status,
            success=success,
        )

        self.assertEqual(status, "failed")
        self.assertFalse(success)
        self.assertEqual(evidence, {})

    def test_verified_success_has_no_mismatch_evidence(
        self,
    ) -> None:
        raw = {
            "status": "succeeded",
            "artifact_validation_passed": True,
            "build_exit_code": 0,
            "target_arch": "aarch64",
            "message": "build completed successfully",
            "artifacts": [
                {
                    "kind": "binary",
                    "path": "artifacts/example_1.0_arm64.deb",
                }
            ],
        }

        status, success, _ = (
            _classify_validator_result(raw)
        )
        evidence = _validator_outcome_evidence(
            raw,
            status=status,
            success=success,
        )

        self.assertEqual(status, "succeeded")
        self.assertTrue(success)
        self.assertEqual(evidence, {})

    def test_build_result_serializes_evidence(
        self,
    ) -> None:
        evidence = {
            "schema_version": "0.1",
            "kind": "artifact_contract_mismatch",
            "underlying_build_succeeded": True,
            "artifact_validation_passed": False,
            "produced_binary_artifact_count": 1,
            "package_format": "deb",
            "target_arch": "riscv64",
            "evidence_source": "validator_result",
            "classifier": (
                "docker-debian-artifact-contract-v1"
            ),
        }

        result = BuildResult(
            backend="docker",
            status="artifact_contract_mismatch",
            success=False,
            message="artifact pattern mismatch",
            case_id="example-riscv64-case",
            target_arch="riscv64",
            exit_code=0,
            artifacts=[
                {
                    "kind": "binary",
                    "path": (
                        "artifacts/"
                        "example_1.0_riscv64.deb"
                    ),
                }
            ],
            outcome_evidence=evidence,
        )

        payload = json.loads(result.to_json())

        self.assertEqual(
            payload["outcome_evidence"],
            evidence,
        )

    def test_server_parser_preserves_evidence(
        self,
    ) -> None:
        evidence = {
            "schema_version": "0.1",
            "kind": "artifact_contract_mismatch",
            "underlying_build_succeeded": True,
            "artifact_validation_passed": False,
            "produced_binary_artifact_count": 1,
            "package_format": "deb",
            "target_arch": "riscv64",
            "evidence_source": "validator_result",
            "classifier": (
                "docker-debian-artifact-contract-v1"
            ),
        }

        payload = {
            "success": False,
            "status": "artifact_contract_mismatch",
            "outcome_evidence": evidence,
        }

        parsed = _parse_build_result_payload(
            json.dumps(payload)
        )

        self.assertFalse(parsed["success"])
        self.assertEqual(
            parsed["status"],
            "artifact_contract_mismatch",
        )
        self.assertEqual(
            parsed["outcome_evidence"],
            evidence,
        )

    def test_server_parser_rejects_non_dict_evidence(
        self,
    ) -> None:
        parsed = _parse_build_result_payload(
            json.dumps(
                {
                    "success": False,
                    "status": "failed",
                    "outcome_evidence": [
                        "not",
                        "trusted",
                    ],
                }
            )
        )

        self.assertEqual(
            parsed["outcome_evidence"],
            {},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
