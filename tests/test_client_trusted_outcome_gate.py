from __future__ import annotations

import inspect
import json
import unittest

from client_patch import AutoRepairClientPatch


class ClientTrustedOutcomeGateTests(unittest.TestCase):
    def mismatch_payload(
        self,
        package_format: str = "deb",
        target_arch: str = "riscv64",
    ) -> dict:
        return {
            "success": False,
            "status": "artifact_contract_mismatch",
            "outcome_evidence": {
                "schema_version": "0.1",
                "kind": "artifact_contract_mismatch",
                "underlying_build_succeeded": True,
                "artifact_validation_passed": False,
                "produced_binary_artifact_count": 2,
                "package_format": package_format,
                "target_arch": target_arch,
                "evidence_source": "validator_result",
                "classifier": "test-classifier-v1",
            },
        }

    def test_trusted_deb_riscv64_is_terminal(self) -> None:
        outcome = AutoRepairClientPatch._trusted_terminal_outcome(
            self.mismatch_payload()
        )
        self.assertEqual(
            outcome,
            "artifact_contract_mismatch",
        )

    def test_rpm_uses_same_client_gate(self) -> None:
        outcome = AutoRepairClientPatch._trusted_terminal_outcome(
            self.mismatch_payload(
                package_format="rpm",
                target_arch="aarch64",
            )
        )
        self.assertEqual(
            outcome,
            "artifact_contract_mismatch",
        )

    def test_future_format_uses_same_client_gate(self) -> None:
        outcome = AutoRepairClientPatch._trusted_terminal_outcome(
            self.mismatch_payload(
                package_format="future-format",
                target_arch="x86_64",
            )
        )
        self.assertEqual(
            outcome,
            "artifact_contract_mismatch",
        )

    def test_status_without_evidence_is_not_terminal(self) -> None:
        outcome = AutoRepairClientPatch._trusted_terminal_outcome(
            {
                "success": False,
                "status": "artifact_contract_mismatch",
            }
        )
        self.assertIsNone(outcome)

    def test_underlying_failure_is_not_terminal(self) -> None:
        payload = self.mismatch_payload()
        payload["outcome_evidence"][
            "underlying_build_succeeded"
        ] = False

        self.assertIsNone(
            AutoRepairClientPatch._trusted_terminal_outcome(
                payload
            )
        )

    def test_artifact_validation_success_is_not_mismatch(
        self,
    ) -> None:
        payload = self.mismatch_payload()
        payload["outcome_evidence"][
            "artifact_validation_passed"
        ] = True

        self.assertIsNone(
            AutoRepairClientPatch._trusted_terminal_outcome(
                payload
            )
        )

    def test_zero_binary_artifacts_is_not_terminal(
        self,
    ) -> None:
        payload = self.mismatch_payload()
        payload["outcome_evidence"][
            "produced_binary_artifact_count"
        ] = 0

        self.assertIsNone(
            AutoRepairClientPatch._trusted_terminal_outcome(
                payload
            )
        )

    def test_boolean_count_is_rejected(self) -> None:
        payload = self.mismatch_payload()
        payload["outcome_evidence"][
            "produced_binary_artifact_count"
        ] = True

        self.assertIsNone(
            AutoRepairClientPatch._trusted_terminal_outcome(
                payload
            )
        )

    def test_normal_failure_is_not_terminal(self) -> None:
        self.assertIsNone(
            AutoRepairClientPatch._trusted_terminal_outcome(
                {
                    "success": False,
                    "status": "failed",
                    "outcome_evidence": {},
                }
            )
        )

    def test_gate_updates_loop_state(self) -> None:
        loop_result = {
            "tool_rounds": 0,
            "build_status": "failed",
            "stop_reason": None,
            "terminal_outcome": None,
        }

        applied = (
            AutoRepairClientPatch
            ._apply_trusted_terminal_outcome(
                self.mismatch_payload(),
                loop_result,
                tool_rounds=4,
            )
        )

        self.assertTrue(applied)
        self.assertEqual(
            loop_result["tool_rounds"],
            4,
        )
        self.assertEqual(
            loop_result["build_status"],
            "artifact_contract_mismatch",
        )
        self.assertEqual(
            loop_result["stop_reason"],
            "artifact_contract_mismatch",
        )
        self.assertEqual(
            loop_result["terminal_outcome"],
            "artifact_contract_mismatch",
        )

    def test_untrusted_result_does_not_mutate_state(
        self,
    ) -> None:
        loop_result = {
            "tool_rounds": 2,
            "build_status": "failed",
            "stop_reason": None,
            "terminal_outcome": None,
        }
        before = dict(loop_result)

        applied = (
            AutoRepairClientPatch
            ._apply_trusted_terminal_outcome(
                {
                    "success": False,
                    "status": "artifact_contract_mismatch",
                    "outcome_evidence": {},
                },
                loop_result,
                tool_rounds=3,
            )
        )

        self.assertFalse(applied)
        self.assertEqual(loop_result, before)

    def test_status_parser_preserves_mismatch(self) -> None:
        payload = self.mismatch_payload()

        self.assertEqual(
            AutoRepairClientPatch._build_status_from_text(
                json.dumps(payload)
            ),
            "artifact_contract_mismatch",
        )

    def test_mismatch_is_effective_validator_run(self) -> None:
        skipped, effective = (
            AutoRepairClientPatch._build_execution_state(
                json.dumps(
                    {
                        "success": False,
                        "status": "artifact_contract_mismatch",
                        "duration_seconds": 10,
                        "exit_code": 0,
                    }
                )
            )
        )

        self.assertFalse(skipped)
        self.assertTrue(effective)

    def test_both_build_paths_use_trusted_gate(self) -> None:
        source = inspect.getsource(
            AutoRepairClientPatch._llm_tools_loop
        )

        self.assertGreaterEqual(
            source.count(
                "_apply_trusted_terminal_outcome"
            ),
            2,
        )

    def test_package_loop_has_terminal_final_status(
        self,
    ) -> None:
        source = inspect.getsource(
            AutoRepairClientPatch.process_one_package
        )

        self.assertIn(
            'terminal_outcome == "artifact_contract_mismatch"',
            source,
        )
        self.assertIn(
            '"artifact_contract_mismatch",\n'
            '                "artifact_contract_mismatch"',
            source,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
