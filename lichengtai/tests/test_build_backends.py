from __future__ import annotations

import json
import hashlib
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.build_backends.docker import DockerBuildBackend
from tools.build_backends.config import load_build_config
from tools.build_backends.models import BuildRequest
from tools.build_backends.obs import ObsBuildBackend
from tools.build_backends.workspace import prepare_agent_workspace


def _write_standard_case(root: Path) -> Path:
    case = root / "case-one"
    (case / "input").mkdir(parents=True)
    (case / "config").mkdir()
    (case / "dependencies" / "rpms").mkdir(parents=True)
    (case / "logs").mkdir()
    (case / "input" / "package.spec").write_text("original\n", encoding="utf-8")
    (case / "input" / "source.tar.gz").write_bytes(b"source")
    (case / "config" / "buildconfig").write_text("Macros:\n", encoding="utf-8")
    (case / "logs" / "original-target-failed.log").write_text(
        "historical failure\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": "1.0",
        "case_id": "case-one",
        "package_type": "rpm",
        "build": {
            "spec": "input/package.spec",
            "buildconfig": "config/buildconfig",
            "dependency_dirs": ["dependencies/rpms"],
            "architecture": "x86_64",
            "timeout_seconds": 60,
            "jobs": 1,
            "vm_type": "docker:privileged",
        },
        "patch_policy": {
            "allowed_paths": ["input/**"],
            "forbidden_paths": ["manifest.json"],
        },
        "expected_artifacts": {"binary": ["*.rpm"], "source": ["*.src.rpm"]},
        "security_mode": "trusted_baseline_only",
    }
    (case / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return case


def _write_fake_validator(root: Path, status: str) -> Path:
    script = root / f"fake-validator-{status}.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import argparse, json
            from pathlib import Path

            parser = argparse.ArgumentParser()
            parser.add_argument('--worker-mode')
            parser.add_argument('--input', required=True)
            parser.add_argument('--output', required=True)
            args = parser.parse_args()
            case = Path(args.input)
            output = Path(args.output)
            output.mkdir(parents=True)
            (output / 'artifacts').mkdir()
            (output / 'observed-worker-mode.txt').write_text(
                (args.worker_mode or 'default') + '\\n',
                encoding='utf-8',
            )
            observed = (case / 'input' / 'package.spec').read_text(encoding='utf-8')
            (output / 'observed-spec.txt').write_text(observed, encoding='utf-8')
            source_map = case / 'evidence' / 'source-filename-map.json'
            if source_map.is_file():
                entries = json.loads(source_map.read_text(encoding='utf-8'))
                (output / 'observed-map-sha256.txt').write_text(
                    entries[0]['sha256'], encoding='utf-8'
                )
            (output / 'build.log').write_text('current {status} log\\n', encoding='utf-8')
            result = {{
                'case_id': 'case-one',
                'status': '{status}',
                'message': '{status}',
                'artifact_validation_passed': {str(status == 'succeeded')},
                'build_exit_code': {0 if status == 'succeeded' else 1},
                'timed_out': False,
                'duration_seconds': 2,
                'target_arch': 'x86_64',
                'artifacts': [],
            }}
            (output / 'build-result.json').write_text(json.dumps(result), encoding='utf-8')
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


class WorkspaceTests(unittest.TestCase):
    def test_standard_case_is_flattened_for_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = _write_standard_case(root)
            workspace = root / "workspace"
            prepare_agent_workspace(case, workspace)

            self.assertEqual(
                (workspace / "package.spec").read_text(encoding="utf-8"),
                "original\n",
            )
            self.assertEqual(
                (workspace / "log_failed.txt").read_text(encoding="utf-8"),
                "historical failure\n",
            )
            metadata = json.loads(
                (workspace / ".buildbench-case.json").read_text(encoding="utf-8")
            )
            self.assertEqual(Path(metadata["source_case_dir"]), case.resolve())
            self.assertFalse((workspace / "dependencies").exists())



def _write_deb_case_for_input_guard(
    root: Path,
    *,
    corrupt_archive: bool = False,
) -> Path:
    import hashlib

    case = _write_standard_case(root)
    input_dir = case / "input"

    payload = b"example source archive\n"
    archive = input_dir / "example_1.0.orig.tar.gz"
    archive.write_bytes(payload)

    dsc = input_dir / "example_1.0-1.dsc"
    dsc.write_text(
        f"""Format: 3.0 (quilt)
Source: example
Binary: example
Architecture: any
Version: 1.0-1
Maintainer: Build-Bench Test <test@example.com>
Checksums-Sha256:
 {hashlib.sha256(payload).hexdigest()} {len(payload)} {archive.name}
Files:
 {hashlib.md5(payload).hexdigest()} {len(payload)} {archive.name}

""",
        encoding="utf-8",
    )

    if corrupt_archive:
        archive.write_bytes(payload + b"corrupted\n")

    manifest_path = case / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["package_type"] = "deb"
    manifest.setdefault("build", {})
    manifest["build"]["backend"] = "deb-obs-build"
    manifest["build"]["recipe"] = f"input/{dsc.name}"
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return case


def _write_counting_validator(root: Path) -> tuple[Path, Path]:
    script = root / "counting-validator.py"
    counter = root / "validator-calls.txt"

    script.write_text(
        f"""import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--worker-mode", default="default")
parser.add_argument("--input", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()

counter = Path({str(counter)!r})
with counter.open("a", encoding="utf-8") as stream:
    stream.write("called\\n")

case = Path(args.input)
output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)

manifest = json.loads(
    (case / "manifest.json").read_text(encoding="utf-8")
)
result = {{
    "status": "succeeded",
    "artifact_validation_passed": True,
    "case_id": manifest.get("case_id", "case-one"),
    "target_arch": manifest.get("build", {{}}).get("architecture"),
    "build_exit_code": 0,
    "timed_out": False,
    "artifacts": [],
}}
(output / "build-result.json").write_text(
    json.dumps(result),
    encoding="utf-8",
)
(output / "build.log").write_text(
    "fake validator succeeded\\n",
    encoding="utf-8",
)
""",
        encoding="utf-8",
    )
    return script, counter


def _write_missing_result_validator(
    root: Path,
) -> tuple[Path, Path]:
    script = root / "missing-result-validator.py"
    counter = root / "missing-result-validator-calls.txt"

    script.write_text(
        f"""import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--worker-mode", default="default")
parser.add_argument("--input", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()

counter = Path({str(counter)!r})
with counter.open("a", encoding="utf-8") as stream:
    stream.write("called\\n")

Path(args.output).mkdir(parents=True, exist_ok=True)
""",
        encoding="utf-8",
    )
    return script, counter


def _validator_call_count(counter: Path) -> int:
    if not counter.is_file():
        return 0
    return len(counter.read_text(encoding="utf-8").splitlines())


class BackendConfigTests(unittest.TestCase):
    def test_environment_can_select_backend_and_server_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "info.yaml"
            config.write_text(
                "build:\n  backend: obs\n  docker:\n    case_store_dir: old\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "BUILD_BENCH_BACKEND": "docker",
                    "BUILD_BENCH_CASE_STORE": "/new/cases",
                    "BUILD_BENCH_WORKER_MODE": "isolated-chroot",
                },
                clear=False,
            ):
                loaded = load_build_config(config)

            self.assertEqual(loaded["backend"], "docker")
            self.assertEqual(loaded["docker"]["case_store_dir"], "/new/cases")
            self.assertEqual(
                loaded["docker"]["worker_mode"],
                "isolated-chroot",
            )


class DockerBackendTests(unittest.TestCase):
    def _backend(
        self,
        root: Path,
        script: Path,
        worker_mode: str | None = None,
    ) -> DockerBuildBackend:
        config = {
            "validator_command": f'python3 "{script}"',
            "result_root": str(root / "results"),
            "staging_root": str(root / "staging"),
        }

        if worker_mode is not None:
            config["worker_mode"] = worker_mode

        return DockerBuildBackend(config)

    def test_default_worker_mode_preserves_validator_default(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = _write_standard_case(root)
            workspace = root / "workspace"
            prepare_agent_workspace(case, workspace)

            backend = self._backend(
                root,
                _write_fake_validator(root, "succeeded"),
            )
            result = backend.build(
                BuildRequest(
                    package_name="case-one",
                    workspace=workspace,
                )
            )

            self.assertTrue(result.success)
            self.assertEqual(
                (
                    Path(result.result_path)
                    / "observed-worker-mode.txt"
                ).read_text(encoding="utf-8"),
                "default\n",
            )

    def test_explicit_isolated_worker_mode_is_forwarded(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = _write_standard_case(root)
            workspace = root / "workspace"
            prepare_agent_workspace(case, workspace)

            backend = self._backend(
                root,
                _write_fake_validator(root, "succeeded"),
                worker_mode="isolated-chroot",
            )
            result = backend.build(
                BuildRequest(
                    package_name="case-one",
                    workspace=workspace,
                )
            )

            self.assertTrue(result.success)
            self.assertEqual(
                (
                    Path(result.result_path)
                    / "observed-worker-mode.txt"
                ).read_text(encoding="utf-8"),
                "isolated-chroot\n",
            )

    def test_invalid_worker_mode_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            with self.assertRaisesRegex(
                RuntimeError,
                "Unsupported Docker worker mode",
            ):
                self._backend(
                    root,
                    _write_fake_validator(root, "succeeded"),
                    worker_mode="automatic-privileged-fallback",
                )

    def test_agent_workspace_overlays_canonical_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = _write_standard_case(root)
            workspace = root / "workspace"
            prepare_agent_workspace(case, workspace)
            (workspace / "package.spec").write_text("agent repair\n", encoding="utf-8")

            backend = self._backend(root, _write_fake_validator(root, "succeeded"))
            result = backend.build(
                BuildRequest(package_name="case-one", workspace=workspace)
            )

            self.assertTrue(result.success)
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(
                (Path(result.result_path) / "observed-spec.txt").read_text(encoding="utf-8"),
                "agent repair\n",
            )
            self.assertEqual(
                (case / "input" / "package.spec").read_text(encoding="utf-8"),
                "original\n",
            )
            staging_parent = root / "staging" / "case-one"
            self.assertFalse(
                staging_parent.exists() and any(staging_parent.iterdir())
            )

    def test_failed_build_log_is_returned_to_agent_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = _write_standard_case(root)
            workspace = root / "workspace"
            prepare_agent_workspace(case, workspace)

            backend = self._backend(root, _write_fake_validator(root, "failed"))
            result = backend.build(
                BuildRequest(package_name="case-one", workspace=workspace)
            )

            self.assertFalse(result.success)
            self.assertEqual(result.status, "failed")
            self.assertEqual(
                (workspace / "log_failed.txt").read_text(encoding="utf-8"),
                "current failed log\n",
            )

    def test_agent_edit_refreshes_only_staged_obs_source_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = _write_standard_case(root)
            (case / "evidence").mkdir()
            original_hash = hashlib.sha256(b"original\n").hexdigest()
            source_map = [
                {
                    "obs_name": "package.spec",
                    "packaged_name": "package.spec",
                    "sha256": original_hash,
                }
            ]
            map_path = case / "evidence" / "source-filename-map.json"
            map_path.write_text(json.dumps(source_map), encoding="utf-8")

            workspace = root / "workspace"
            prepare_agent_workspace(case, workspace)
            repaired = "agent repair\n"
            (workspace / "package.spec").write_text(repaired, encoding="utf-8")

            backend = self._backend(root, _write_fake_validator(root, "succeeded"))
            result = backend.build(
                BuildRequest(package_name="case-one", workspace=workspace)
            )

            self.assertTrue(result.success)
            observed_hash = (
                Path(result.result_path) / "observed-map-sha256.txt"
            ).read_text(encoding="utf-8")
            self.assertEqual(
                observed_hash,
                hashlib.sha256(repaired.encode("utf-8")).hexdigest(),
            )
            canonical_map = json.loads(map_path.read_text(encoding="utf-8"))
            self.assertEqual(canonical_map[0]["sha256"], original_hash)

    def test_input_guard_is_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = _write_deb_case_for_input_guard(root)
            workspace = root / "workspace"
            prepare_agent_workspace(case, workspace)
            script, counter = _write_counting_validator(root)

            first = self._backend(root, script)
            second = self._backend(root, script)

            first_result = first.build(
                BuildRequest("case-one", workspace)
            )
            second_result = second.build(
                BuildRequest("case-one", workspace)
            )

            self.assertTrue(first_result.success)
            self.assertTrue(second_result.success)
            self.assertEqual(_validator_call_count(counter), 2)

    def test_valid_dsc_preflight_allows_validator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = _write_deb_case_for_input_guard(root)
            workspace = root / "workspace"
            prepare_agent_workspace(case, workspace)
            script, counter = _write_counting_validator(root)

            backend = DockerBuildBackend(
                {
                    "validator_command": f'python3 "{script}"',
                    "result_root": str(root / "results"),
                    "staging_root": str(root / "staging"),
                    "input_guard_enabled": True,
                }
            )
            result = backend.build(
                BuildRequest("case-one", workspace)
            )

            self.assertTrue(result.success)
            self.assertEqual(_validator_call_count(counter), 1)

    def test_dsc_checksum_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = _write_deb_case_for_input_guard(
                root,
                corrupt_archive=True,
            )
            workspace = root / "workspace"
            prepare_agent_workspace(case, workspace)
            script, counter = _write_counting_validator(root)

            backend = DockerBuildBackend(
                {
                    "validator_command": f'python3 "{script}"',
                    "result_root": str(root / "results"),
                    "staging_root": str(root / "staging"),
                    "input_guard_enabled": True,
                }
            )
            result = backend.build(
                BuildRequest("case-one", workspace)
            )

            self.assertFalse(result.success)
            self.assertEqual(result.status, "preflight_rejected")
            self.assertEqual(_validator_call_count(counter), 0)

    def test_unchanged_input_is_skipped_across_backend_instances(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = _write_deb_case_for_input_guard(root)
            workspace = root / "workspace"
            prepare_agent_workspace(case, workspace)
            script, counter = _write_counting_validator(root)

            config = {
                "validator_command": f'python3 "{script}"',
                "result_root": str(root / "results"),
                "staging_root": str(root / "staging"),
                "input_guard_enabled": True,
            }
            first = DockerBuildBackend(config)
            second = DockerBuildBackend(config)

            first_result = first.build(
                BuildRequest("case-one", workspace)
            )
            second_result = second.build(
                BuildRequest("case-one", workspace)
            )

            self.assertTrue(first_result.success)
            self.assertFalse(second_result.success)
            self.assertEqual(
                second_result.status,
                "skipped_unchanged_input",
            )
            self.assertEqual(_validator_call_count(counter), 1)

    def test_changed_input_runs_validator_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = _write_deb_case_for_input_guard(root)
            workspace = root / "workspace"
            prepare_agent_workspace(case, workspace)
            script, counter = _write_counting_validator(root)

            config = {
                "validator_command": f'python3 "{script}"',
                "result_root": str(root / "results"),
                "staging_root": str(root / "staging"),
                "input_guard_enabled": True,
            }
            first = DockerBuildBackend(config)
            first_result = first.build(
                BuildRequest("case-one", workspace)
            )
            self.assertTrue(first_result.success)

            (workspace / "agent-change.patch").write_text(
                "updated repair input\n",
                encoding="utf-8",
            )

            second = DockerBuildBackend(config)
            second_result = second.build(
                BuildRequest("case-one", workspace)
            )

            self.assertTrue(second_result.success)
            self.assertEqual(_validator_call_count(counter), 2)

    def test_missing_result_does_not_poison_input_guard_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = _write_deb_case_for_input_guard(root)
            workspace = root / "workspace"
            prepare_agent_workspace(case, workspace)
            script, counter = _write_missing_result_validator(root)

            config = {
                "validator_command": f'python3 "{script}"',
                "result_root": str(root / "results"),
                "staging_root": str(root / "staging"),
                "input_guard_enabled": True,
            }

            first = DockerBuildBackend(config)
            second = DockerBuildBackend(config)

            first_result = first.build(
                BuildRequest("case-one", workspace)
            )
            second_result = second.build(
                BuildRequest("case-one", workspace)
            )

            self.assertFalse(first_result.success)
            self.assertFalse(second_result.success)
            self.assertEqual(first_result.status, "infrastructure_error")
            self.assertEqual(second_result.status, "infrastructure_error")
            self.assertEqual(_validator_call_count(counter), 2)

    def test_validator_failure_statuses_are_preserved(self) -> None:
        for status in (
            "unresolvable",
            "timeout",
            "invalid_patch",
            "infrastructure_error",
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                case = _write_standard_case(root)
                workspace = root / "workspace"
                prepare_agent_workspace(case, workspace)
                backend = self._backend(root, _write_fake_validator(root, status))

                result = backend.build(
                    BuildRequest(package_name="case-one", workspace=workspace)
                )

                self.assertFalse(result.success)
                self.assertEqual(result.status, status)


class ObsBackendCompatibilityTests(unittest.TestCase):
    @patch("tools.build_backends.obs.check_main", return_value="Build succeeded!")
    @patch("tools.build_backends.obs.main_upload", return_value="Success: uploaded")
    def test_original_obs_backend_is_still_available(
        self, _upload: object, _check: object
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result = ObsBuildBackend().build(
                BuildRequest(package_name="pkg", workspace=workspace)
            )

        self.assertTrue(result.success)
        self.assertEqual(result.backend, "obs")
        self.assertEqual(result.status, "succeeded")


if __name__ == "__main__":
    unittest.main()
