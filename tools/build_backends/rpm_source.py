"""Safe RPM source materialization and staged rebuild support."""

from __future__ import annotations

import bz2
import gzip
import hashlib
import json
import lzma
import os
import shutil
import tarfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

STATE_NAME = ".buildbench-rpm-source.json"
SOURCE_TREE_NAME = "rpm_source"
SUPPORTED_ARCHIVES = {
    ".tar.gz": "tar.gz",
    ".tgz": "tar.gz",
    ".tar.xz": "tar.xz",
    ".txz": "tar.xz",
    ".tar.bz2": "tar.bz2",
    ".tbz": "tar.bz2",
    ".tbz2": "tar.bz2",
}
MAX_MEMBER_COUNT = 100_000
MAX_TOTAL_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024


class RpmSourceError(RuntimeError):
    """A safe, user-facing RPM source lifecycle failure."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(COPY_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_fingerprint(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise RpmSourceError(f"Source tree is not a normal directory: {root}")

    digest = hashlib.sha256()
    items = [root]
    items.extend(
        sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    )

    for item in items:
        relative = "." if item == root else item.relative_to(root).as_posix()
        digest.update(f"\0path:{relative}\0".encode("utf-8"))

        if item.is_symlink():
            raise RpmSourceError(f"Symlinks are not supported in RPM V1 source trees: {item}")
        if item.is_dir():
            digest.update(b"directory\0")
            digest.update(str(item.stat().st_mode & 0o7777).encode("ascii"))
        elif item.is_file():
            digest.update(b"file\0")
            digest.update(str(item.stat().st_mode & 0o7777).encode("ascii"))
            digest.update(b"\0")
            digest.update(_sha256(item).encode("ascii"))
        else:
            raise RpmSourceError(f"Unsupported special file in source tree: {item}")

    return digest.hexdigest()


def _archive_format(path: Path) -> str | None:
    lowered = path.name.lower()
    for extension, archive_format in sorted(
        SUPPORTED_ARCHIVES.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if lowered.endswith(extension):
            return archive_format
    return None


def _load_case_manifest(workspace: Path) -> dict[str, Any]:
    metadata_path = workspace / ".buildbench-case.json"
    if not metadata_path.is_file() or metadata_path.is_symlink():
        raise RpmSourceError(
            "RPM V1 requires a canonical Case workspace with .buildbench-case.json"
        )

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RpmSourceError(f"Cannot read workspace Case metadata: {error}") from error

    source_case_value = metadata.get("source_case_dir")
    if not isinstance(source_case_value, str) or not source_case_value:
        raise RpmSourceError("Workspace Case metadata has no source_case_dir")

    source_case = Path(source_case_value).expanduser().resolve()
    manifest_path = source_case / "manifest.json"

    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RpmSourceError(f"Canonical Case manifest is missing: {manifest_path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RpmSourceError(f"Cannot read canonical Case manifest: {error}") from error

    if not isinstance(manifest, dict):
        raise RpmSourceError("Canonical Case manifest is not a JSON object")
    if str(manifest.get("package_type", "")).strip().lower() != "rpm":
        raise RpmSourceError("prepare_rpm_source_tool is only valid for RPM Cases")

    return manifest


def _candidate_archives(workspace: Path) -> list[Path]:
    candidates: list[Path] = []

    for item in sorted(workspace.iterdir(), key=lambda path: path.name):
        if (
            item.is_file()
            and not item.is_symlink()
            and _archive_format(item) is not None
        ):
            candidates.append(item)

    return candidates


def _safe_member_parts(member: tarfile.TarInfo) -> tuple[str, ...]:
    raw_name = member.name

    if not raw_name or "\x00" in raw_name:
        raise RpmSourceError("Archive contains an empty or NUL-containing member name")
    if raw_name.startswith(("/", "\\")):
        raise RpmSourceError(f"Archive contains an absolute member path: {raw_name!r}")
    if "\\" in raw_name:
        raise RpmSourceError(
            f"Archive member uses a non-POSIX path separator: {raw_name!r}"
        )

    pure = PurePosixPath(raw_name)
    parts = tuple(part for part in pure.parts if part not in ("", "."))

    if not parts or any(part == ".." for part in parts):
        raise RpmSourceError(f"Archive contains an unsafe member path: {raw_name!r}")
    if any(part in (".", "..") for part in raw_name.split("/")):
        raise RpmSourceError(f"Archive contains a traversal component: {raw_name!r}")

    return parts


def _validated_members(
    archive: tarfile.TarFile,
) -> tuple[list[tuple[tarfile.TarInfo, tuple[str, ...]]], str]:
    members = archive.getmembers()

    if not members:
        raise RpmSourceError("Source archive is empty")
    if len(members) > MAX_MEMBER_COUNT:
        raise RpmSourceError(
            f"Source archive has {len(members)} members; limit is {MAX_MEMBER_COUNT}"
        )

    validated: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
    seen_paths: set[tuple[str, ...]] = set()
    top_directories: set[str] = set()
    total_size = 0

    for member in members:
        parts = _safe_member_parts(member)

        if parts in seen_paths:
            raise RpmSourceError(
                f"Source archive contains a duplicate member path: {member.name!r}"
            )
        seen_paths.add(parts)
        top_directories.add(parts[0])

        if member.issym() or member.islnk():
            raise RpmSourceError(
                f"Source archive links are not supported in RPM V1: {member.name!r}"
            )
        if not member.isdir() and not member.isreg():
            raise RpmSourceError(
                f"Source archive contains a special member: {member.name!r}"
            )
        if len(parts) == 1 and not member.isdir():
            raise RpmSourceError(
                "RPM V1 requires all archive content beneath one top-level directory"
            )
        if member.size < 0:
            raise RpmSourceError(f"Archive member has a negative size: {member.name!r}")

        total_size += member.size
        if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise RpmSourceError(
                "Source archive exceeds the RPM V1 uncompressed-size limit"
            )

        validated.append((member, parts))

    if len(top_directories) != 1:
        raise RpmSourceError(
            "RPM V1 requires exactly one top-level source directory; found "
            + ", ".join(sorted(top_directories))
        )

    top_directory = next(iter(top_directories))
    return validated, top_directory


def _open_tar_for_read(path: Path) -> tarfile.TarFile:
    archive_format = _archive_format(path)
    modes = {
        "tar.gz": "r:gz",
        "tar.xz": "r:xz",
        "tar.bz2": "r:bz2",
    }
    mode = modes.get(archive_format)

    if mode is None:
        raise RpmSourceError(f"Unsupported RPM source archive: {path.name}")

    try:
        return tarfile.open(path, mode)
    except (OSError, tarfile.TarError) as error:
        raise RpmSourceError(f"Cannot open source archive {path.name}: {error}") from error


def _extract_validated_archive(archive_path: Path, destination: Path) -> str:
    destination.mkdir(parents=False)

    try:
        with _open_tar_for_read(archive_path) as archive:
            members, top_directory = _validated_members(archive)

            for member, parts in members:
                target = destination.joinpath(*parts)

                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    os.chmod(target, member.mode & 0o7777)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                if stream is None:
                    raise RpmSourceError(
                        f"Cannot read regular archive member: {member.name!r}"
                    )

                with stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=COPY_CHUNK_BYTES)

                if target.stat().st_size != member.size:
                    raise RpmSourceError(
                        f"Extracted size mismatch for archive member: {member.name!r}"
                    )
                os.chmod(target, member.mode & 0o7777)

        source_root = destination / top_directory
        if not source_root.is_dir() or source_root.is_symlink():
            raise RpmSourceError(
                "Source archive did not produce one normal top-level directory"
            )

        return top_directory
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")

    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RpmSourceError(f"Cannot read RPM source state: {error}") from error

    if not isinstance(state, dict) or state.get("schema") != 1:
        raise RpmSourceError("Unsupported RPM source materialization state")
    if state.get("source_dir") != SOURCE_TREE_NAME:
        raise RpmSourceError("Unexpected RPM source tree path in state")

    archive_name = state.get("archive")
    if (
        not isinstance(archive_name, str)
        or Path(archive_name).name != archive_name
        or _archive_format(Path(archive_name)) is None
    ):
        raise RpmSourceError("Unsafe or unsupported archive name in RPM state")

    archive_format = state.get("archive_format")
    if archive_format != _archive_format(Path(archive_name)):
        raise RpmSourceError("RPM state archive format does not match its filename")

    top_directory = state.get("top_directory")
    if (
        not isinstance(top_directory, str)
        or not top_directory
        or Path(top_directory).name != top_directory
        or top_directory in {".", ".."}
    ):
        raise RpmSourceError("Unsafe top-level source directory in RPM state")

    for field in ("initial_archive_sha256", "initial_tree_sha256"):
        value = state.get(field)
        if not isinstance(value, str) or len(value) != 64:
            raise RpmSourceError(f"Missing {field} in RPM source state")

    return state


def prepare_rpm_source_workspace(package_path: str) -> dict[str, Any]:
    """Materialize one canonical RPM Case source archive without changing inputs."""

    root = Path(package_path).expanduser().resolve()
    state_path = root / STATE_NAME
    source_container = root / SOURCE_TREE_NAME
    temporary_tree: Path | None = None
    created_tree = False

    try:
        if not root.is_dir() or root.is_symlink():
            raise RpmSourceError(f"Package workspace is not a normal directory: {root}")

        _load_case_manifest(root)

        if (
            state_path.is_file()
            and not state_path.is_symlink()
            and source_container.is_dir()
            and not source_container.is_symlink()
        ):
            state = _load_state(state_path)
            source_root = source_container / state["top_directory"]
            if not source_root.is_dir() or source_root.is_symlink():
                raise RpmSourceError(
                    "RPM state exists but its active source directory is incomplete"
                )
            return {
                "success": True,
                "status": "already_materialized",
                "archive": str(root / state["archive"]),
                "archive_format": state["archive_format"],
                "source_tree": str(source_root),
                "message": "Existing materialized RPM source tree was preserved",
            }

        if state_path.exists() or source_container.exists():
            raise RpmSourceError(
                "Incomplete RPM source materialization exists; refusing to delete or overwrite it"
            )

        archives = _candidate_archives(root)
        if len(archives) != 1:
            raise RpmSourceError(
                f"Expected exactly one top-level RPM source archive, found {len(archives)}"
            )
        archive_path = archives[0]
        archive_format = _archive_format(archive_path)
        if archive_format is None:
            raise RpmSourceError(f"Unsupported RPM source archive: {archive_path.name}")

        temporary_tree = root / f".{SOURCE_TREE_NAME}.{uuid.uuid4().hex}.tmp"
        top_directory = _extract_validated_archive(archive_path, temporary_tree)
        source_root = temporary_tree / top_directory
        initial_tree_sha256 = _tree_fingerprint(source_root)
        initial_archive_sha256 = _sha256(archive_path)

        os.replace(temporary_tree, source_container)
        temporary_tree = None
        created_tree = True

        state = {
            "schema": 1,
            "archive": archive_path.name,
            "archive_format": archive_format,
            "source_dir": SOURCE_TREE_NAME,
            "top_directory": top_directory,
            "initial_archive_sha256": initial_archive_sha256,
            "initial_tree_sha256": initial_tree_sha256,
        }
        _write_json_atomic(state_path, state)

        active_source_root = source_container / top_directory
        return {
            "success": True,
            "status": "materialized",
            "archive": str(archive_path),
            "archive_format": archive_format,
            "source_tree": str(active_source_root),
            "initial_archive_sha256": initial_archive_sha256,
            "initial_tree_sha256": initial_tree_sha256,
            "message": (
                "Modify only the returned source_tree, then call "
                "run_build_validation_tool"
            ),
        }
    except (RpmSourceError, OSError, tarfile.TarError) as error:
        if temporary_tree is not None:
            shutil.rmtree(temporary_tree, ignore_errors=True)
        if created_tree and not state_path.exists():
            shutil.rmtree(source_container, ignore_errors=True)
        return {
            "success": False,
            "status": "prepare_failed",
            "message": str(error),
        }


def _write_tar_members(archive: tarfile.TarFile, source_root: Path) -> None:
    top_directory = source_root.name
    items = [source_root]
    items.extend(
        sorted(
            source_root.rglob("*"),
            key=lambda item: item.relative_to(source_root).as_posix(),
        )
    )

    for item in items:
        if item.is_symlink():
            raise RpmSourceError(
                f"Symlinks are not supported in RPM V1 source trees: {item}"
            )

        relative = (
            PurePosixPath(top_directory)
            if item == source_root
            else PurePosixPath(top_directory)
            / PurePosixPath(item.relative_to(source_root).as_posix())
        )
        info = tarfile.TarInfo(relative.as_posix())
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        info.mode = item.stat().st_mode & 0o7777

        if item.is_dir():
            info.type = tarfile.DIRTYPE
            info.size = 0
            archive.addfile(info)
        elif item.is_file():
            info.type = tarfile.REGTYPE
            info.size = item.stat().st_size
            with item.open("rb") as handle:
                archive.addfile(info, handle)
        else:
            raise RpmSourceError(f"Unsupported special file in source tree: {item}")


def _write_deterministic_archive(
    source_root: Path,
    destination: Path,
    archive_format: str,
) -> None:
    raw: BinaryIO | None = None

    try:
        raw = destination.open("xb")

        if archive_format == "tar.gz":
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw,
                mtime=0,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    _write_tar_members(archive, source_root)
        elif archive_format == "tar.xz":
            with lzma.LZMAFile(raw, mode="w", format=lzma.FORMAT_XZ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    _write_tar_members(archive, source_root)
        elif archive_format == "tar.bz2":
            with bz2.BZ2File(raw, mode="w") as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    _write_tar_members(archive, source_root)
        else:
            raise RpmSourceError(f"Unsupported RPM archive format: {archive_format}")
    finally:
        if raw is not None and not raw.closed:
            raw.close()


def _rebuild(
    workspace: Path,
    staged_case: Path,
) -> tuple[bool, str | None]:
    state_path = workspace / STATE_NAME

    if not state_path.exists():
        return False, None
    if not state_path.is_file() or state_path.is_symlink():
        raise RpmSourceError("RPM source state is not a normal file")

    state = _load_state(state_path)
    source_root = (
        workspace
        / SOURCE_TREE_NAME
        / state["top_directory"]
    )
    current_fingerprint = _tree_fingerprint(source_root)

    if current_fingerprint == state["initial_tree_sha256"]:
        return False, None

    input_dir = (staged_case / "input").resolve()
    archive_path = input_dir / state["archive"]

    if not archive_path.is_file() or archive_path.is_symlink():
        raise RpmSourceError(f"Staged RPM source archive is missing: {state['archive']}")
    if _sha256(archive_path) != state["initial_archive_sha256"]:
        raise RpmSourceError(
            "Staged RPM source archive no longer matches the materialized input"
        )

    temporary_archive = archive_path.with_name(
        f".{archive_path.name}.{uuid.uuid4().hex}.tmp"
    )

    try:
        _write_deterministic_archive(
            source_root,
            temporary_archive,
            state["archive_format"],
        )
        os.replace(temporary_archive, archive_path)
        return True, None
    finally:
        temporary_archive.unlink(missing_ok=True)


def rebuild_rpm_source_if_modified(
    workspace: Path,
    staged_case: Path,
) -> tuple[bool, str | None]:
    """Repack a modified RPM source tree only inside the temporary staged Case."""

    try:
        return _rebuild(workspace.resolve(), staged_case.resolve())
    except (RpmSourceError, OSError, ValueError, tarfile.TarError) as error:
        return False, f"RPM source rebuild failed: {error}"
