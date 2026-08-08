from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from tools.build_backends.docker import _rebuild_materialized_source
from tools.build_backends.rpm_source import (
    STATE_NAME,
    SOURCE_TREE_NAME,
    prepare_rpm_source_workspace,
    rebuild_rpm_source_if_modified,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_case_manifest(case: Path) -> None:
    (case / "input").mkdir(parents=True)
    (case / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "case_id": case.name,
                "package_type": "rpm",
                "build": {
                    "spec": "input/example.spec",
                    "architecture": "aarch64",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_workspace_metadata(workspace: Path, case: Path) -> None:
    (workspace / ".buildbench-case.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source_case_dir": str(case.resolve()),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_tar_gz(
    path: Path,
    *,
    member_name: str = "example-1.0/source.txt",
    content: bytes = b"old\n",
    symlink: bool = False,
) -> None:
    with tarfile.open(path, "w:gz") as archive:
        root = tarfile.TarInfo("example-1.0")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)

        member = tarfile.TarInfo(member_name)
        member.mode = 0o644

        if symlink:
            member.type = tarfile.SYMTYPE
            member.linkname = "/tmp/outside"
            member.size = 0
            archive.addfile(member)
        else:
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))


class RpmSourceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rpm-source-lifecycle-test-"
        )
        self.root = Path(self.temporary.name)
        self.case = self.root / "case-one"
        self.workspace = self.root / "workspace"
        self.staged = self.root / "staged"

        _write_case_manifest(self.case)
        self.workspace.mkdir()
        _write_workspace_metadata(self.workspace, self.case)
        (self.workspace / "example.spec").write_text(
            "Name: example\nSource0: example-1.0.tar.gz\n",
            encoding="utf-8",
        )
        self.archive = self.workspace / "example-1.0.tar.gz"
        _write_tar_gz(self.archive)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _stage_workspace_input(self, destination: Path) -> None:
        (destination / "input").mkdir(parents=True)
        shutil.copy2(
            self.workspace / "example.spec",
            destination / "input" / "example.spec",
        )
        shutil.copy2(
            self.workspace / "example-1.0.tar.gz",
            destination / "input" / "example-1.0.tar.gz",
        )
        shutil.copy2(
            self.case / "manifest.json",
            destination / "manifest.json",
        )

    def test_prepare_materializes_one_active_source_root(self) -> None:
        original_hash = _sha256(self.archive)
        result = prepare_rpm_source_workspace(str(self.workspace))

        self.assertTrue(result["success"], result)
        self.assertEqual(result["status"], "materialized")
        source_tree = Path(result["source_tree"])
        self.assertEqual(
            source_tree,
            self.workspace / SOURCE_TREE_NAME / "example-1.0",
        )
        self.assertEqual(
            (source_tree / "source.txt").read_text(encoding="utf-8"),
            "old\n",
        )
        self.assertTrue((self.workspace / STATE_NAME).is_file())
        self.assertEqual(_sha256(self.archive), original_hash)

    def test_modified_tree_rebuilds_only_staged_archive(self) -> None:
        result = prepare_rpm_source_workspace(str(self.workspace))
        self.assertTrue(result["success"], result)
        source_tree = Path(result["source_tree"])
        original_hash = _sha256(self.archive)

        (source_tree / "source.txt").write_text("new\n", encoding="utf-8")
        self._stage_workspace_input(self.staged)

        rebuilt, error = _rebuild_materialized_source(
            self.workspace,
            self.staged,
        )

        self.assertTrue(rebuilt)
        self.assertIsNone(error)
        self.assertEqual(_sha256(self.archive), original_hash)
        staged_archive = self.staged / "input" / "example-1.0.tar.gz"
        self.assertNotEqual(_sha256(staged_archive), original_hash)

        with tarfile.open(staged_archive, "r:gz") as archive:
            extracted = archive.extractfile("example-1.0/source.txt")
            self.assertIsNotNone(extracted)
            assert extracted is not None
            self.assertEqual(extracted.read(), b"new\n")

    def test_rebuild_is_deterministic_for_same_tree(self) -> None:
        result = prepare_rpm_source_workspace(str(self.workspace))
        self.assertTrue(result["success"], result)
        source_tree = Path(result["source_tree"])
        (source_tree / "source.txt").write_text("new\n", encoding="utf-8")

        staged_one = self.root / "staged-one"
        staged_two = self.root / "staged-two"
        self._stage_workspace_input(staged_one)
        self._stage_workspace_input(staged_two)

        first, first_error = rebuild_rpm_source_if_modified(
            self.workspace,
            staged_one,
        )
        second, second_error = rebuild_rpm_source_if_modified(
            self.workspace,
            staged_two,
        )

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertIsNone(first_error)
        self.assertIsNone(second_error)
        self.assertEqual(
            _sha256(staged_one / "input" / "example-1.0.tar.gz"),
            _sha256(staged_two / "input" / "example-1.0.tar.gz"),
        )

    def test_unchanged_tree_does_not_repack(self) -> None:
        result = prepare_rpm_source_workspace(str(self.workspace))
        self.assertTrue(result["success"], result)
        self._stage_workspace_input(self.staged)

        rebuilt, error = rebuild_rpm_source_if_modified(
            self.workspace,
            self.staged,
        )

        self.assertFalse(rebuilt)
        self.assertIsNone(error)

    def test_prepare_rejects_multiple_archives(self) -> None:
        _write_tar_gz(self.workspace / "second.tar.gz")

        result = prepare_rpm_source_workspace(str(self.workspace))

        self.assertFalse(result["success"])
        self.assertIn("exactly one", result["message"])

    def test_prepare_rejects_path_traversal(self) -> None:
        self.archive.unlink()
        with tarfile.open(self.archive, "w:gz") as archive:
            member = tarfile.TarInfo("../outside.txt")
            payload = b"bad\n"
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))

        result = prepare_rpm_source_workspace(str(self.workspace))

        self.assertFalse(result["success"])
        self.assertIn("unsafe", result["message"].lower())
        self.assertFalse((self.root / "outside.txt").exists())

    def test_prepare_rejects_symlink_members(self) -> None:
        self.archive.unlink()
        _write_tar_gz(
            self.archive,
            member_name="example-1.0/link",
            symlink=True,
        )

        result = prepare_rpm_source_workspace(str(self.workspace))

        self.assertFalse(result["success"])
        self.assertIn("links are not supported", result["message"])


if __name__ == "__main__":
    unittest.main()
