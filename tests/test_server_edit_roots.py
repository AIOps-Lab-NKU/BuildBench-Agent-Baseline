from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _install_stub_module(name: str, **attributes: object) -> None:
    module = types.ModuleType(name)

    for key, value in attributes.items():
        setattr(module, key, value)

    sys.modules[name] = module


def _fake_structure(path: str) -> dict[str, object]:
    return {
        "success": True,
        "root": str(Path(path).resolve()),
    }


def _fake_prepare_workspace(
    source: Path,
    destination: Path,
) -> None:
    shutil.copytree(source, destination, dirs_exist_ok=True)


_install_stub_module(
    "tools.auto_repair.get_repo_structure",
    get_project_structure_from_local=_fake_structure,
)
_install_stub_module(
    "tools.auto_repair.check_build_res",
    check_main=lambda *_args, **_kwargs: None,
)
_install_stub_module(
    "tools.auto_repair.upload_files",
    main_upload=lambda *_args, **_kwargs: None,
)
_install_stub_module(
    "tools.build_backends.service",
    run_build_validation=lambda *_args, **_kwargs: None,
)
_install_stub_module(
    "tools.build_backends.debian_source",
    prepare_debian_source_workspace=lambda *_args, **_kwargs: {
        "success": False,
        "status": "not_configured",
    },
    rebuild_debian_source_if_modified=lambda *_args, **_kwargs: (
        False,
        None,
    ),
)
_install_stub_module(
    "tools.build_backends.rpm_source",
    prepare_rpm_source_workspace=lambda *_args, **_kwargs: {
        "success": False,
        "status": "not_configured",
    },
    rebuild_rpm_source_if_modified=lambda *_args, **_kwargs: (
        False,
        None,
    ),
)
_install_stub_module(
    "tools.build_backends.workspace",
    prepare_agent_workspace=_fake_prepare_workspace,
)

sys.modules.pop("server_patch", None)

import server_patch


class ServerEditRootBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        server_patch.server_state["modification_history"] = {}
        server_patch.server_state["tool_call_history"] = {}
        server_patch.server_state["tool_cache"] = {}
        server_patch.server_state["workspace_sources"] = {}
        server_patch.server_state["workspace_edit_roots"] = {}

        repo_root = Path(__file__).resolve().parents[1]

        self.temporary = tempfile.TemporaryDirectory(
            prefix=".edit-root-test-",
            dir=str(repo_root),
        )
        self.root = Path(self.temporary.name)
        self.base = self.root / "base"
        self.work = self.root / "work"
        self.results = self.root / "results"
        self.case_id = "case-a"
        self.source_case = self.base / self.case_id

        self.source_case.mkdir(parents=True)
        (self.source_case / "top-level.txt").write_text(
            "top-level\n",
            encoding="utf-8",
        )

        initialized = json.loads(
            server_patch.init_package_environment_tool(
                str(self.base),
                self.case_id,
                str(self.work),
                str(self.results),
            )
        )

        self.assertTrue(initialized["success"])
        self.workspace = Path(
            initialized["package_path"]
        ).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _activate_debian_source(self) -> Path:
        source_tree = self.workspace / "debian_source"
        source_tree.mkdir()
        (source_tree / "source.c").write_text(
            "old\n",
            encoding="utf-8",
        )

        prepared = {
            "success": True,
            "status": "materialized",
            "source_tree": str(source_tree),
        }

        with mock.patch.object(
            server_patch,
            "prepare_debian_source_workspace",
            return_value=prepared,
        ):
            result = json.loads(
                server_patch.prepare_debian_source_tool(
                    str(self.workspace)
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(
            Path(result["active_edit_root"]).resolve(),
            source_tree.resolve(),
        )

        return source_tree.resolve()

    def test_initial_workspace_is_the_active_edit_root(self) -> None:
        workspace_real = str(self.workspace)

        self.assertEqual(
            server_patch.server_state["workspace_edit_roots"][
                workspace_real
            ],
            workspace_real,
        )

        content = server_patch.get_file_content_tool(
            str(self.workspace / "top-level.txt")
        )
        self.assertEqual(content, "top-level\n")

    def test_reads_use_workspace_scope_after_prepare(self) -> None:
        source_tree = self._activate_debian_source()

        workspace_read = server_patch.get_file_content_tool(
            str(self.workspace / "top-level.txt")
        )
        self.assertEqual(workspace_read, "top-level\n")

        source_read = server_patch.get_file_content_tool(
            str(source_tree / "source.c")
        )
        self.assertEqual(source_read, "old\n")

        workspace_structure = server_patch.get_structure_of_files(
            str(self.workspace)
        )
        self.assertTrue(workspace_structure["success"])
        self.assertEqual(
            Path(workspace_structure["root"]).resolve(),
            self.workspace,
        )

        source_structure = server_patch.get_structure_of_files(
            str(source_tree)
        )
        self.assertTrue(source_structure["success"])
        self.assertEqual(
            Path(source_structure["root"]).resolve(),
            source_tree,
        )

    def test_patch_root_must_equal_active_edit_root(self) -> None:
        source_tree = self._activate_debian_source()

        blocked = server_patch.apply_git_unified_patch_tool(
            repo_root=str(self.workspace),
            mode="exact_replace",
            file_path="top-level.txt",
            old_text="top-level",
            new_text="changed",
        )
        self.assertIn(
            "repo_root must equal the active edit root",
            blocked,
        )

        applied = server_patch.apply_git_unified_patch_tool(
            repo_root=str(source_tree),
            mode="exact_replace",
            file_path="source.c",
            old_text="old",
            new_text="new",
        )

        self.assertTrue(applied.startswith("Success: exact_replace"))
        self.assertEqual(
            (source_tree / "source.c").read_text(encoding="utf-8"),
            "new\n",
        )

    def test_history_is_keyed_by_case_not_debian_source(self) -> None:
        source_tree = self._activate_debian_source()

        result = server_patch.apply_git_unified_patch_tool(
            repo_root=str(source_tree),
            mode="exact_replace",
            file_path="source.c",
            old_text="old",
            new_text="new",
        )

        self.assertTrue(result.startswith("Success: exact_replace"))
        history = server_patch.server_state["modification_history"]

        self.assertIn(self.case_id, history)
        self.assertNotIn("debian_source", history)
        self.assertEqual(
            history[self.case_id][0]["file_path"],
            "source.c",
        )


    def test_build_feedback_is_published_inside_workspace(self) -> None:
        self._activate_debian_source()

        backend_result = self.root / "backend-result"
        backend_result.mkdir()

        build_log = backend_result / "build.log"
        build_log.write_text(
            "Traceback\n"
            "  File \"bdsf/wavelet_atrous.py\", line 22\n"
            "ImportError: cannot import name '_centered'\n",
            encoding="utf-8",
        )

        diagnostics = backend_result / "build-diagnostics.json"
        diagnostics.write_text(
            '{"fatal_file": "bdsf/wavelet_atrous.py", '
            '"fatal_line": 22}\n',
            encoding="utf-8",
        )

        case_id = self.case_id

        class FakeBuildResult:
            def to_dict(self) -> dict[str, object]:
                return {
                    "backend": "docker",
                    "status": "failed",
                    "success": False,
                    "message": "build failed",
                    "case_id": case_id,
                    "target_arch": "aarch64",
                    "exit_code": 1,
                    "timed_out": False,
                    "duration_seconds": 12,
                    "log_path": str(build_log),
                    "result_path": str(backend_result),
                    "artifacts": [],
                }

        with mock.patch.object(
            server_patch,
            "run_build_validation",
            return_value=FakeBuildResult(),
        ):
            payload = json.loads(
                server_patch.run_build_validation_tool(
                    str(self.workspace)
                )
            )

        self.assertEqual(payload["status"], "failed")

        feedback_dir = Path(
            payload["feedback_dir"]
        ).resolve()
        self.assertTrue(
            feedback_dir.is_relative_to(self.workspace)
        )

        feedback_log = Path(
            payload["feedback_log_path"]
        ).resolve()

        readable = server_patch.get_file_content_tool(
            str(feedback_log)
        )
        self.assertIn("wavelet_atrous.py", readable)
        self.assertIn("_centered", readable)

        summary_path = Path(
            payload["feedback_summary_path"]
        )
        diagnostics_path = Path(
            payload["feedback_diagnostics_path"]
        )

        self.assertTrue(summary_path.is_file())
        self.assertTrue(diagnostics_path.is_file())

    def test_skipped_build_preserves_latest_effective_feedback(
        self,
    ) -> None:
        feedback_dir = (
            self.workspace
            / ".buildbench-feedback"
            / "latest"
        )
        feedback_dir.mkdir(parents=True)

        existing_log = feedback_dir / "build-log-tail.txt"
        existing_log.write_text(
            "existing effective failure\n",
            encoding="utf-8",
        )

        class FakeSkippedResult:
            def to_dict(self) -> dict[str, object]:
                return {
                    "backend": "docker",
                    "status": "skipped_unchanged_input",
                    "success": False,
                    "message": "unchanged",
                    "case_id": self_case_id,
                    "target_arch": None,
                    "exit_code": None,
                    "timed_out": False,
                    "duration_seconds": None,
                    "log_path": None,
                    "result_path": None,
                    "artifacts": [],
                }

        self_case_id = self.case_id

        with mock.patch.object(
            server_patch,
            "run_build_validation",
            return_value=FakeSkippedResult(),
        ):
            payload = json.loads(
                server_patch.run_build_validation_tool(
                    str(self.workspace)
                )
            )

        self.assertEqual(
            payload["status"],
            "skipped_unchanged_input",
        )
        self.assertEqual(
            existing_log.read_text(encoding="utf-8"),
            "existing effective failure\n",
        )
        self.assertEqual(
            Path(payload["feedback_log_path"]).resolve(),
            existing_log.resolve(),
        )

    def test_unregistered_external_file_is_not_readable(self) -> None:
        external = self.root / "outside.txt"
        external.write_text("outside\n", encoding="utf-8")

        result = server_patch.get_file_content_tool(str(external))

        self.assertIn(
            "outside every registered Agent workspace",
            result,
        )



class ServerRpmEditRootBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        server_patch.server_state["modification_history"] = {}
        server_patch.server_state["tool_call_history"] = {}
        server_patch.server_state["tool_cache"] = {}
        server_patch.server_state["workspace_sources"] = {}
        server_patch.server_state["workspace_edit_roots"] = {}

        repo_root = Path(__file__).resolve().parents[1]
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".rpm-edit-root-test-",
            dir=str(repo_root),
        )
        self.root = Path(self.temporary.name)
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()

        workspace_real = str(self.workspace)
        server_patch.server_state["workspace_sources"][workspace_real] = workspace_real
        server_patch.server_state["workspace_edit_roots"][workspace_real] = workspace_real

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_prepare_rpm_source_registers_exact_active_root(self) -> None:
        source_tree = (
            self.workspace / "rpm_source" / "example-1.0"
        ).resolve()
        source_tree.mkdir(parents=True)
        (source_tree / "source.c").write_text(
            "old\n",
            encoding="utf-8",
        )

        prepared = {
            "success": True,
            "status": "materialized",
            "source_tree": str(source_tree),
        }

        with mock.patch.object(
            server_patch,
            "prepare_rpm_source_workspace",
            return_value=prepared,
        ):
            result = json.loads(
                server_patch.prepare_rpm_source_tool(
                    str(self.workspace)
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(
            Path(result["active_edit_root"]).resolve(),
            source_tree,
        )
        self.assertEqual(
            server_patch.server_state["workspace_edit_roots"][
                str(self.workspace)
            ],
            str(source_tree),
        )

        blocked = server_patch.apply_git_unified_patch_tool(
            repo_root=str(self.workspace),
            mode="exact_replace",
            file_path="top-level.txt",
            old_text="old",
            new_text="new",
        )
        self.assertIn(
            "repo_root must equal the active edit root",
            blocked,
        )

        applied = server_patch.apply_git_unified_patch_tool(
            repo_root=str(source_tree),
            mode="exact_replace",
            file_path="source.c",
            old_text="old",
            new_text="new",
        )
        self.assertTrue(
            applied.startswith("Success: exact_replace")
        )
        self.assertEqual(
            (source_tree / "source.c").read_text(
                encoding="utf-8"
            ),
            "new\n",
        )

if __name__ == "__main__":
    unittest.main()
