"""Adapter from a Build-Bench Agent workspace to Docker Validator v0."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .debian_source import rebuild_debian_source_if_modified
from .rpm_source import rebuild_rpm_source_if_modified
from .models import BuildRequest, BuildResult


_WORKSPACE_METADATA = ".buildbench-case.json"
_WORKSPACE_EXCLUDES = {
    _WORKSPACE_METADATA,
    "log_failed.txt",
    "manifest.json",
    ".buildbench-debian-source.json",
    "debian_source",
    ".buildbench-rpm-source.json",
    "rpm_source",
    "extracted",
    "result",
    "results",
    "artifacts",
}


def _copy_or_link(source: str, destination: str) -> str:
    try:
        os.link(source, destination)
        return destination
    except OSError:
        return shutil.copy2(source, destination)


def _copy_case(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        copy_function=_copy_or_link,
    )


def _copy_workspace_input(workspace: Path, input_dir: Path) -> None:
    if input_dir.exists():
        shutil.rmtree(input_dir)
    input_dir.mkdir(parents=True)

    for source in workspace.iterdir():
        if source.name in _WORKSPACE_EXCLUDES or source.name.startswith(".build-case-"):
            continue
        destination = input_dir / source.name
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        elif source.is_file():
            shutil.copy2(source, destination)


def _rebuild_materialized_source(
    workspace: Path,
    staged_case: Path,
) -> tuple[bool, str | None]:
    """Route a materialized source tree by the staged canonical Case type."""

    manifest_path = staged_case / "manifest.json"

    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        return False, f"Source lifecycle routing failed: {error}"

    if not isinstance(manifest, dict):
        return False, "Source lifecycle routing failed: manifest is not an object"

    package_type = str(
        manifest.get("package_type", "")
    ).strip().lower()
    build = manifest.get("build")
    backend = (
        str(build.get("backend", "")).strip().lower()
        if isinstance(build, dict)
        else ""
    )

    if package_type == "rpm":
        return rebuild_rpm_source_if_modified(
            workspace,
            staged_case,
        )

    if package_type == "deb" or backend == "deb-obs-build":
        return rebuild_debian_source_if_modified(
            workspace,
            staged_case,
        )

    return False, None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _refresh_staged_source_map(staged_case: Path) -> None:
    """Keep an ephemeral OBS filename map consistent with Agent edits.

    Canonical Cases retain archival checksums in ``evidence``.  The Docker
    Validator verifies those checksums while materializing Windows-safe OBS
    filenames.  Once an Agent changes an input file, the staged (temporary)
    copy must describe the changed bytes; otherwise a legitimate repair is
    rejected before the build starts.  The canonical Case is never modified.
    """

    map_path = staged_case / "evidence" / "source-filename-map.json"
    if not map_path.is_file():
        return

    entries = json.loads(map_path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        return

    input_dir = staged_case / "input"
    refreshed: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            refreshed.append(entry)
            continue
        packaged_name = entry.get("packaged_name")
        if not isinstance(packaged_name, str):
            refreshed.append(entry)
            continue
        source = input_dir / packaged_name
        if not source.is_file() or source.is_symlink():
            # An Agent may legitimately remove an input file.  Keeping the
            # archival entry would make the temporary Case impossible to open.
            continue
        updated = dict(entry)
        updated["sha256"] = _sha256(source)
        refreshed.append(updated)

    # ``_copy_case`` may hard-link immutable Case assets for efficiency.
    # Replace the staged map atomically instead of overwriting its inode, or
    # the canonical archival map would be changed through that hard link.
    temporary_map = map_path.with_name(
        f".{map_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary_map.write_text(
            json.dumps(refreshed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_map, map_path)
    finally:
        temporary_map.unlink(missing_ok=True)


def _load_workspace_metadata(workspace: Path) -> dict[str, Any]:
    metadata_path = workspace / _WORKSPACE_METADATA
    if not metadata_path.is_file():
        return {}
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Invalid workspace metadata: {metadata_path}") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid workspace metadata: {metadata_path}")
    return data



def _validate_debian_source_checksums(staged_case: Path) -> str | None:
    """Return an error when a Debian recipe fails its SHA-256 preflight."""

    manifest_path = staged_case / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return f"Cannot read staged manifest: {error}"

    build = manifest.get("build")
    if not isinstance(build, dict):
        build = {}

    package_type = str(manifest.get("package_type", "")).strip().lower()
    backend = str(build.get("backend", "")).strip().lower()
    if package_type != "deb" and backend != "deb-obs-build":
        return None

    recipe_value = build.get("recipe")
    if not isinstance(recipe_value, str) or not recipe_value.strip():
        return "Debian Case does not declare build.recipe"

    recipe = Path(recipe_value)
    if recipe.is_absolute() or recipe.suffix.lower() != ".dsc":
        return f"Debian build.recipe is not a relative .dsc path: {recipe_value!r}"

    input_root = (staged_case / "input").resolve()
    dsc_path = (staged_case / recipe).resolve()
    try:
        dsc_path.relative_to(input_root)
    except ValueError:
        return f"Debian recipe escapes the staged input directory: {recipe_value!r}"

    if not dsc_path.is_file():
        return f"Debian recipe does not exist: {recipe_value!r}"

    try:
        lines = dsc_path.read_text(
            encoding="utf-8",
            errors="strict",
        ).splitlines()
    except (OSError, UnicodeError) as error:
        return f"Cannot read Debian recipe {recipe_value!r}: {error}"

    in_sha256 = False
    saw_sha256 = False
    entry_count = 0

    for line in lines:
        if not in_sha256:
            if line.strip() == "Checksums-Sha256:":
                in_sha256 = True
                saw_sha256 = True
            continue

        if not line.startswith((" ", "\t")):
            break

        stripped = line.strip()
        if not stripped:
            continue

        fields = stripped.split(maxsplit=2)
        if len(fields) != 3:
            return f"Malformed Checksums-Sha256 entry in {dsc_path.name}"

        expected_hash, expected_size_text, filename = fields
        if (
            len(expected_hash) != 64
            or any(character not in "0123456789abcdefABCDEF"
                   for character in expected_hash)
        ):
            return f"Invalid SHA-256 value for {filename!r}"

        try:
            expected_size = int(expected_size_text)
        except ValueError:
            return f"Invalid file size for {filename!r}"

        referenced_name = Path(filename)
        if referenced_name.is_absolute():
            return f"Absolute filename in Checksums-Sha256: {filename!r}"

        referenced = (dsc_path.parent / referenced_name).resolve()
        try:
            referenced.relative_to(input_root)
        except ValueError:
            return f"Checksummed file escapes staged input: {filename!r}"

        if not referenced.is_file():
            return f"Checksummed source file is missing: {filename!r}"

        actual_size = referenced.stat().st_size
        if actual_size != expected_size:
            return (
                f"Size mismatch for {filename!r}: "
                f"expected {expected_size}, got {actual_size}"
            )

        actual_hash = _sha256(referenced)
        if actual_hash.lower() != expected_hash.lower():
            return (
                f"SHA-256 mismatch for {filename!r}: "
                f"expected {expected_hash.lower()}, got {actual_hash}"
            )

        entry_count += 1

    if not saw_sha256 or entry_count == 0:
        return f"No usable Checksums-Sha256 entries in {dsc_path.name}"

    return None


def _fingerprint_staged_build(
    staged_case: Path,
    validator_command: list[str],
    worker_mode: str,
) -> str:
    """Fingerprint the effective Case input and build context."""

    validator_files = []
    for token in validator_command:
        candidate = Path(token).expanduser()
        if candidate.is_file():
            validator_files.append(
                {
                    "path": str(candidate.resolve()),
                    "sha256": _sha256(candidate),
                }
            )

    context = {
        "schema": 1,
        "validator_command": validator_command,
        "validator_files": validator_files,
        "worker_mode": worker_mode,
    }

    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )

    effective_roots = (
        "manifest.json",
        "input",
        "config",
        "dependencies",
        "evidence/source-filename-map.json",
    )

    for relative_root in effective_roots:
        root = staged_case / relative_root
        digest.update(f"\0root:{relative_root}\0".encode("utf-8"))

        if not root.exists() and not root.is_symlink():
            digest.update(b"missing")
            continue

        items = [root]
        if root.is_dir() and not root.is_symlink():
            items.extend(
                sorted(
                    root.rglob("*"),
                    key=lambda item: item.relative_to(staged_case).as_posix(),
                )
            )

        for item in items:
            relative = item.relative_to(staged_case).as_posix()
            digest.update(f"\0path:{relative}\0".encode("utf-8"))

            if item.is_symlink():
                digest.update(b"symlink\0")
                digest.update(os.readlink(item).encode("utf-8"))
            elif item.is_dir():
                digest.update(b"directory")
            elif item.is_file():
                digest.update(b"file\0")
                digest.update(
                    str(item.stat().st_mode & 0o7777).encode("ascii")
                )
                digest.update(b"\0")
                digest.update(_sha256(item).encode("ascii"))
            else:
                digest.update(b"special")

    return digest.hexdigest()


def _input_guard_state_path(
    state_root: Path,
    package_name: str,
) -> Path:
    key = hashlib.sha256(package_name.encode("utf-8")).hexdigest()
    return state_root / f"{key}.json"


def _read_input_guard_state(
    state_path: Path,
    package_name: str,
) -> str | None:
    if not state_path.is_file():
        return None

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid input-guard state {state_path}: {error}") from error

    if not isinstance(state, dict):
        raise ValueError(f"Invalid input-guard state object: {state_path}")
    if state.get("package_name") != package_name:
        raise ValueError(f"Input-guard state identity mismatch: {state_path}")

    fingerprint = state.get("fingerprint")
    if not isinstance(fingerprint, str):
        raise ValueError(f"Input-guard state has no fingerprint: {state_path}")

    return fingerprint


def _write_input_guard_state(
    state_path: Path,
    package_name: str,
    fingerprint: str,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_name(
        f".{state_path.name}.{uuid.uuid4().hex}.tmp"
    )

    payload = {
        "schema_version": 1,
        "package_name": package_name,
        "fingerprint": fingerprint,
        "updated_at_epoch": time.time(),
    }

    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, state_path)
    finally:
        temporary.unlink(missing_ok=True)



def _classify_validator_result(
    raw: dict[str, Any],
) -> tuple[str, bool, str]:
    """Classify a parsed Docker Validator result.

    A Debian build can complete successfully and produce binary packages while
    artifact validation still fails because the Case manifest expects the
    wrong binary-package filename. Keep this distinct from a source/build
    failure so the Agent is not encouraged to rewrite otherwise valid package
    metadata solely to satisfy an incorrect artifact pattern.
    """

    status = str(raw.get("status", "infrastructure_error"))
    artifact_validation_passed = bool(
        raw.get("artifact_validation_passed", False)
    )
    message = str(raw.get("message", status))

    if status == "succeeded" and artifact_validation_passed:
        return status, True, message

    artifacts = raw.get("artifacts")
    has_debian_binary = (
        isinstance(artifacts, list)
        and any(
            isinstance(artifact, dict)
            and artifact.get("kind") == "binary"
            and isinstance(artifact.get("path"), str)
            and artifact["path"].lower().endswith((".deb", ".ddeb"))
            for artifact in artifacts
        )
    )

    artifact_contract_mismatch = (
        raw.get("build_exit_code") == 0
        and not artifact_validation_passed
        and has_debian_binary
        and "expected binary artifact pattern" in message
        and "was not matched" in message
    )

    if artifact_contract_mismatch:
        guidance = (
            "The package build completed and Debian binary artifacts were "
            "produced, but the expected-artifact pattern did not match them. "
            "Treat this as an artifact contract mismatch, not a source build "
            "failure. Do not modify source code or Debian package metadata "
            "solely to satisfy the conflicting pattern."
        )
        return (
            "artifact_contract_mismatch",
            False,
            f"{message}. {guidance}",
        )

    return status, False, message


def _validator_outcome_evidence(
    raw: dict[str, Any],
    *,
    status: str,
    success: bool,
) -> dict[str, Any]:
    """Return package-format-neutral evidence for a trusted outcome.

    Package-format-specific recognition remains inside the backend.
    The Client consumes only the generic evidence semantics.
    """

    if (
        status != "artifact_contract_mismatch"
        or success
    ):
        return {}

    artifacts = raw.get("artifacts")

    if not isinstance(artifacts, list):
        return {}

    produced_binary_artifacts = [
        artifact
        for artifact in artifacts
        if (
            isinstance(artifact, dict)
            and artifact.get("kind") == "binary"
            and isinstance(artifact.get("path"), str)
            and artifact["path"].lower().endswith(
                (".deb", ".ddeb")
            )
        )
    ]

    if not produced_binary_artifacts:
        return {}

    return {
        "schema_version": "0.1",
        "kind": "artifact_contract_mismatch",
        "underlying_build_succeeded": (
            raw.get("build_exit_code") == 0
        ),
        "artifact_validation_passed": bool(
            raw.get(
                "artifact_validation_passed",
                False,
            )
        ),
        "produced_binary_artifact_count": len(
            produced_binary_artifacts
        ),
        "package_format": "deb",
        "target_arch": raw.get("target_arch"),
        "evidence_source": "validator_result",
        "classifier": (
            "docker-debian-artifact-contract-v1"
        ),
    }

class DockerBuildBackend:
    name = "docker"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        command = config.get("validator_command")
        if not command:
            raise RuntimeError("build.docker.validator_command is required")
        self.validator_command = shlex.split(str(command))
        self.result_root = Path(
            config.get("result_root", "run_records/docker-builds")
        ).expanduser()
        self.staging_root = Path(
            config.get("staging_root", "temp_workspace/docker-cases")
        ).expanduser()
        self.case_store_dir = (
            Path(str(config["case_store_dir"])).expanduser()
            if config.get("case_store_dir")
            else None
        )
        self.case_map = {
            str(key): Path(str(value)).expanduser()
            for key, value in dict(config.get("case_map") or {}).items()
        }

        raw_input_guard = config.get("input_guard_enabled", False)
        if isinstance(raw_input_guard, bool):
            self.input_guard_enabled = raw_input_guard
        elif isinstance(raw_input_guard, str) and raw_input_guard.lower() in {
            "true",
            "false",
        }:
            self.input_guard_enabled = raw_input_guard.lower() == "true"
        else:
            raise RuntimeError(
                "build.docker.input_guard_enabled must be a boolean"
            )

        configured_state_dir = config.get("input_guard_state_dir")
        self.input_guard_state_dir = (
            Path(str(configured_state_dir)).expanduser()
            if configured_state_dir
            else self.result_root / ".input-guard"
        )

        configured_worker_mode = str(
            config.get("worker_mode", "nested-docker")
        ).strip().lower()

        self.worker_mode = (
            configured_worker_mode or "nested-docker"
        )

        allowed_worker_modes = {
            "nested-docker",
            "isolated-chroot",
        }

        if self.worker_mode not in allowed_worker_modes:
            allowed = ", ".join(sorted(allowed_worker_modes))
            raise RuntimeError(
                "Unsupported Docker worker mode "
                f"{self.worker_mode!r}; expected one of: {allowed}"
            )

    def _resolve_case(self, package_name: str, workspace: Path) -> Path:
        metadata = _load_workspace_metadata(workspace)
        candidates: list[Path] = []
        if metadata.get("source_case_dir"):
            candidates.append(Path(str(metadata["source_case_dir"])).expanduser())
        if package_name in self.case_map:
            candidates.append(self.case_map[package_name])
        if self.case_store_dir is not None:
            candidates.append(self.case_store_dir / package_name)

        for candidate in candidates:
            resolved = candidate.resolve()
            if (resolved / "manifest.json").is_file() and (resolved / "input").is_dir():
                return resolved
        raise RuntimeError(
            f"No canonical Docker Case found for {package_name!r}; "
            "use a standard Case directory or configure build.docker.case_map"
        )

    def build(self, request: BuildRequest) -> BuildResult:
        package_name = request.package_name
        workspace = request.workspace.resolve()
        try:
            source_case = self._resolve_case(package_name, workspace)
        except (OSError, RuntimeError, ValueError) as error:
            return BuildResult(
                backend=self.name,
                status="infrastructure_error",
                success=False,
                message=str(error),
                case_id=package_name,
            )
        stamp = time.strftime("%Y%m%d_%H%M%S")
        run_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        output_dir = (self.result_root / package_name / run_id).resolve()
        staged_case = (self.staging_root / package_name / run_id).resolve()
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staged_case.parent.mkdir(parents=True, exist_ok=True)

        try:
            _copy_case(source_case, staged_case)
            _copy_workspace_input(workspace, staged_case / "input")
            _, rebuild_error = _rebuild_materialized_source(
                workspace,
                staged_case,
            )
            if rebuild_error is not None:
                return BuildResult(
                    backend=self.name,
                    status="source_rebuild_failed",
                    success=False,
                    message=rebuild_error,
                    case_id=package_name,
                    result_path=str(output_dir),
                )
            _refresh_staged_source_map(staged_case)

            guard_fingerprint: str | None = None
            guard_state_path: Path | None = None

            if self.input_guard_enabled:
                preflight_error = _validate_debian_source_checksums(
                    staged_case
                )
                if preflight_error is not None:
                    return BuildResult(
                        backend=self.name,
                        status="preflight_rejected",
                        success=False,
                        message=preflight_error,
                        case_id=package_name,
                        result_path=str(output_dir),
                    )

                guard_fingerprint = _fingerprint_staged_build(
                    staged_case,
                    self.validator_command,
                    self.worker_mode,
                )
                guard_state_path = _input_guard_state_path(
                    self.input_guard_state_dir,
                    package_name,
                )
                previous_fingerprint = _read_input_guard_state(
                    guard_state_path,
                    package_name,
                )

                if previous_fingerprint == guard_fingerprint:
                    return BuildResult(
                        backend=self.name,
                        status="skipped_unchanged_input",
                        success=False,
                        message=(
                            "Validator was not started because the effective "
                            "build input is unchanged from the previous attempt"
                        ),
                        case_id=package_name,
                        result_path=str(output_dir),
                    )

            command = [
                *self.validator_command,
            ]

            # Preserve the Validator's safe/default nested-Docker
            # behavior unless isolated chroot execution is explicitly
            # selected by trusted experiment configuration.
            if self.worker_mode != "nested-docker":
                command.extend(
                    [
                        "--worker-mode",
                        self.worker_mode,
                    ]
                )

            command.extend(
                [
                    "--input",
                    str(staged_case),
                    "--output",
                    str(output_dir),
                ]
            )

            completed = subprocess.run(
                command,
                check=False,
                text=True,
            )

            result_file = output_dir / "build-result.json"
            if not result_file.is_file():
                return BuildResult(
                    backend=self.name,
                    status="infrastructure_error",
                    success=False,
                    message=(
                        "Docker Validator did not produce build-result.json "
                        f"(exit code {completed.returncode})"
                    ),
                    case_id=package_name,
                    exit_code=completed.returncode,
                    result_path=str(output_dir),
                )

            raw = json.loads(result_file.read_text(encoding="utf-8"))
            # Persist the fingerprint only after the Validator produced a
            # result file that was successfully parsed.
            if guard_fingerprint is not None and guard_state_path is not None:
                _write_input_guard_state(
                    guard_state_path,
                    package_name,
                    guard_fingerprint,
                )

            status, success, message = _classify_validator_result(raw)
            outcome_evidence = _validator_outcome_evidence(
                raw,
                status=status,
                success=success,
            )
            log_path = output_dir / "build.log"
            if not success and log_path.is_file():
                shutil.copy2(log_path, workspace / "log_failed.txt")

            return BuildResult(
                backend=self.name,
                status=status,
                success=success,
                message=message,
                case_id=str(raw.get("case_id", package_name)),
                target_arch=raw.get("target_arch"),
                exit_code=raw.get("build_exit_code", completed.returncode),
                timed_out=bool(raw.get("timed_out", False)),
                duration_seconds=raw.get("duration_seconds"),
                log_path=str(log_path) if log_path.is_file() else None,
                result_path=str(output_dir),
                artifacts=list(raw.get("artifacts") or []),
                outcome_evidence=outcome_evidence,
            )
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
            return BuildResult(
                backend=self.name,
                status="infrastructure_error",
                success=False,
                message=f"Docker Validator invocation failed: {error}",
                case_id=package_name,
                result_path=str(output_dir),
            )
        finally:
            shutil.rmtree(staged_case, ignore_errors=True)
