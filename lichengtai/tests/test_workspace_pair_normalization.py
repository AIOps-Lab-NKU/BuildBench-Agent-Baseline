from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.build_backends.workspace import (
    prepare_agent_workspace,
)


def write_canonical_case(
    root: Path,
    *,
    case_id: str,
    payload_name: str,
    payload_text: str,
    failure_text: str,
) -> Path:
    root.mkdir(parents=True)

    input_dir = root / "input"
    input_dir.mkdir()

    (input_dir / payload_name).write_text(
        payload_text,
        encoding="utf-8",
    )

    logs = root / "logs"
    logs.mkdir()

    (
        logs / "original-target-failed.log"
    ).write_text(
        failure_text,
        encoding="utf-8",
    )

    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "case_id": case_id,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    return root


class PairedCaseNormalizationTests(
    unittest.TestCase
):
    def test_target_wrapper_selects_matching_target_leaf(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case_id = "example-amd64-arm64-demo--target"
            wrapper = root / case_id

            source = write_canonical_case(
                wrapper / "source",
                case_id="example-amd64-arm64-demo--source",
                payload_name="source.dsc",
                payload_text="source\n",
                failure_text="source failure\n",
            )

            target = write_canonical_case(
                wrapper / "target",
                case_id=case_id,
                payload_name="target.dsc",
                payload_text="target\n",
                failure_text="target failure\n",
            )

            workspace = root / "workspace"

            prepare_agent_workspace(
                wrapper,
                workspace,
            )

            self.assertFalse(
                (workspace / "source.dsc").exists()
            )
            self.assertEqual(
                (
                    workspace / "target.dsc"
                ).read_text(encoding="utf-8"),
                "target\n",
            )
            self.assertEqual(
                (
                    workspace / "log_failed.txt"
                ).read_text(encoding="utf-8"),
                "target failure\n",
            )

            metadata = json.loads(
                (
                    workspace
                    / ".buildbench-case.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(
                Path(
                    metadata["source_case_dir"]
                ).resolve(),
                target.resolve(),
            )

            self.assertNotEqual(
                Path(
                    metadata["source_case_dir"]
                ).resolve(),
                source.resolve(),
            )

    def test_source_wrapper_selects_matching_source_leaf(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case_id = "example-arm64-amd64-demo--source"
            wrapper = root / case_id

            source = write_canonical_case(
                wrapper / "source",
                case_id=case_id,
                payload_name="source.dsc",
                payload_text="source\n",
                failure_text="source failure\n",
            )

            write_canonical_case(
                wrapper / "target",
                case_id="example-arm64-amd64-demo--target",
                payload_name="target.dsc",
                payload_text="target\n",
                failure_text="target failure\n",
            )

            workspace = root / "workspace"

            prepare_agent_workspace(
                wrapper,
                workspace,
            )

            self.assertEqual(
                (
                    workspace / "source.dsc"
                ).read_text(encoding="utf-8"),
                "source\n",
            )
            self.assertFalse(
                (workspace / "target.dsc").exists()
            )

            metadata = json.loads(
                (
                    workspace
                    / ".buildbench-case.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(
                Path(
                    metadata["source_case_dir"]
                ).resolve(),
                source.resolve(),
            )

    def test_existing_canonical_case_behavior_is_preserved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case_id = "canonical-case"

            case = write_canonical_case(
                root / case_id,
                case_id=case_id,
                payload_name="package.dsc",
                payload_text="canonical\n",
                failure_text="historical failure\n",
            )

            workspace = root / "workspace"

            prepare_agent_workspace(
                case,
                workspace,
            )

            self.assertEqual(
                (
                    workspace / "package.dsc"
                ).read_text(encoding="utf-8"),
                "canonical\n",
            )

            metadata = json.loads(
                (
                    workspace
                    / ".buildbench-case.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(
                Path(
                    metadata["source_case_dir"]
                ).resolve(),
                case.resolve(),
            )

    def test_unmatched_legacy_wrapper_is_copied_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "legacy-package"
            wrapper.mkdir()

            (wrapper / "legacy.txt").write_text(
                "legacy\n",
                encoding="utf-8",
            )

            workspace = root / "workspace"

            prepare_agent_workspace(
                wrapper,
                workspace,
            )

            self.assertEqual(
                (
                    workspace / "legacy.txt"
                ).read_text(encoding="utf-8"),
                "legacy\n",
            )
            self.assertFalse(
                (
                    workspace
                    / ".buildbench-case.json"
                ).exists()
            )


if __name__ == "__main__":
    unittest.main()
