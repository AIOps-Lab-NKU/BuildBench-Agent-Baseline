"""Package-aware Debian source materialization and rebuild support."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any


STATE_NAME = ".buildbench-debian-source.json"
SOURCE_TREE_NAME = "debian_source"
SUPPORTED_FORMAT = "3.0 (quilt)"
COMMAND_TIMEOUT_SECONDS = 180


class DebianSourceError(RuntimeError):
    """A safe, user-facing Debian source lifecycle failure."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_fingerprint(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise DebianSourceError(f"Source tree is not a normal directory: {root}")

    digest = hashlib.sha256()
    items = [root]
    items.extend(
        sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    )
    for item in items:
        relative = "." if item == root else item.relative_to(root).as_posix()
        digest.update(f"\0path:{relative}\0".encode("utf-8"))
        if item.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(item).encode("utf-8"))
        elif item.is_dir():
            digest.update(b"directory")
        elif item.is_file():
            digest.update(b"file\0")
            digest.update(str(item.stat().st_mode & 0o7777).encode("ascii"))
            digest.update(b"\0")
            digest.update(_sha256(item).encode("ascii"))
        else:
            raise DebianSourceError(f"Unsupported special file in source tree: {item}")
    return digest.hexdigest()


def _read_control_fields(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError) as error:
        raise DebianSourceError(f"Cannot read Debian control file {path.name}: {error}") from error

    fields: dict[str, str] = {}
    current: str | None = None
    for line in lines:
        if line.startswith((" ", "\t")):
            if current is not None:
                fields[current] += "\n" + line.strip()
            continue
        if ":" not in line:
            current = None
            continue
        name, value = line.split(":", 1)
        current = name.strip()
        fields[current] = value.strip()
    return fields


def _checksum_filenames(dsc_path: Path) -> list[str]:
    fields = _read_control_fields(dsc_path)
    raw = fields.get("Checksums-Sha256", "")
    names: list[str] = []
    for line in raw.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) != 3:
            continue
        name = parts[2]
        candidate = Path(name)
        if candidate.is_absolute() or len(candidate.parts) != 1 or name in {".", ".."}:
            raise DebianSourceError(f"Unsafe filename in Checksums-Sha256: {name!r}")
        names.append(name)
    if not names:
        raise DebianSourceError(f"No usable Checksums-Sha256 entries in {dsc_path.name}")
    return names


def _run(command: list[str], cwd: Path) -> str:
    environment = os.environ.copy()
    environment.setdefault("DEBFULLNAME", "Build-Bench Agent")
    environment.setdefault("DEBEMAIL", "buildbench-agent@localhost")
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DebianSourceError(f"Cannot run {' '.join(command)}: {error}") from error
    output = completed.stdout or ""
    if completed.returncode != 0:
        raise DebianSourceError(
            f"Command exited with {completed.returncode}: {' '.join(command)}\n"
            + output[-8000:]
        )
    return output[-8000:]


def _load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DebianSourceError(f"Cannot read materialization state: {error}") from error
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise DebianSourceError("Unsupported Debian source materialization state")
    if state.get("format") != SUPPORTED_FORMAT:
        raise DebianSourceError(f"Only Debian source format {SUPPORTED_FORMAT!r} is supported")
    if state.get("source_dir") != SOURCE_TREE_NAME:
        raise DebianSourceError("Unexpected Debian source tree path in state")
    recipe = state.get("recipe")
    if not isinstance(recipe, str) or Path(recipe).name != recipe or not recipe.endswith(".dsc"):
        raise DebianSourceError("Unsafe or missing Debian recipe in state")
    fingerprint = state.get("initial_tree_sha256")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise DebianSourceError("Missing initial source-tree fingerprint")
    return state


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_atomic(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_debian_source_workspace(package_path: str) -> dict[str, Any]:
    """Materialize one Debian 3.0 (quilt) package without changing its inputs."""

    root = Path(package_path).expanduser().resolve()
    state_path = root / STATE_NAME
    source_tree = root / SOURCE_TREE_NAME
    temporary_tree: Path | None = None
    created_tree = False

    try:
        if not root.is_dir() or root.is_symlink():
            raise DebianSourceError(f"Package workspace is not a normal directory: {root}")

        if state_path.is_file() and source_tree.is_dir() and not source_tree.is_symlink():
            state = _load_state(state_path)
            return {
                "success": True,
                "status": "already_materialized",
                "format": state["format"],
                "recipe": str(root / state["recipe"]),
                "source_tree": str(source_tree),
                "message": "Existing materialized source tree was preserved",
            }

        if state_path.exists() or source_tree.exists():
            raise DebianSourceError(
                "Incomplete Debian source materialization exists; refusing to delete or overwrite it"
            )

        recipes = sorted(root.glob("*.dsc"))
        if len(recipes) != 1:
            raise DebianSourceError(
                f"Expected exactly one top-level .dsc recipe, found {len(recipes)}"
            )
        recipe = recipes[0]
        fields = _read_control_fields(recipe)
        source_format = fields.get("Format", "").strip()
        if source_format != SUPPORTED_FORMAT:
            raise DebianSourceError(
                f"Unsupported Debian source format {source_format!r}; "
                f"only {SUPPORTED_FORMAT!r} is enabled in V6"
            )

        temporary_tree = root / f".{SOURCE_TREE_NAME}.{uuid.uuid4().hex}.tmp"
        _run(["dpkg-source", "-x", str(recipe), str(temporary_tree)], root)
        if not (temporary_tree / "debian").is_dir():
            raise DebianSourceError("dpkg-source did not produce a complete Debian source tree")

        fingerprint = _tree_fingerprint(temporary_tree)
        os.replace(temporary_tree, source_tree)
        temporary_tree = None
        created_tree = True
        state = {
            "schema": 1,
            "format": SUPPORTED_FORMAT,
            "recipe": recipe.name,
            "source_dir": SOURCE_TREE_NAME,
            "initial_tree_sha256": fingerprint,
        }
        _write_json_atomic(state_path, state)
        return {
            "success": True,
            "status": "materialized",
            "format": SUPPORTED_FORMAT,
            "recipe": str(recipe),
            "source_tree": str(source_tree),
            "initial_tree_sha256": fingerprint,
            "message": "Modify only the returned source_tree, then call run_build_validation_tool",
        }
    except (DebianSourceError, OSError) as error:
        if temporary_tree is not None:
            shutil.rmtree(temporary_tree, ignore_errors=True)
        if created_tree and not state_path.exists():
            shutil.rmtree(source_tree, ignore_errors=True)
        return {
            "success": False,
            "status": "prepare_failed",
            "message": str(error),
        }


def _rebuild(
    workspace: Path,
    staged_case: Path,
) -> tuple[bool, str | None]:
    state_path = workspace / STATE_NAME
    if not state_path.exists():
        return False, None
    if not state_path.is_file() or state_path.is_symlink():
        raise DebianSourceError("Debian source state is not a normal file")

    state = _load_state(state_path)
    source_tree = workspace / SOURCE_TREE_NAME
    current_fingerprint = _tree_fingerprint(source_tree)
    if current_fingerprint == state["initial_tree_sha256"]:
        return False, None

    input_dir = (staged_case / "input").resolve()
    recipe = input_dir / state["recipe"]
    if not recipe.is_file() or recipe.is_symlink():
        raise DebianSourceError(f"Staged Debian recipe is missing: {state['recipe']}")
    fields = _read_control_fields(recipe)
    if fields.get("Format", "").strip() != SUPPORTED_FORMAT:
        raise DebianSourceError("Staged recipe format no longer matches materialized source")

    rebuild_root = staged_case / f".buildbench-debian-rebuild-{uuid.uuid4().hex}"
    rebuild_root.mkdir(parents=True)
    try:
        for item in input_dir.iterdir():
            if item.is_file() and not item.is_symlink():
                shutil.copy2(item, rebuild_root / item.name)

        build_tree = rebuild_root / "source"
        shutil.copytree(source_tree, build_tree, symlinks=True)
        _run(["dpkg-source", "--auto-commit", "-b", str(build_tree)], rebuild_root)

        generated_recipe = rebuild_root / recipe.name
        if not generated_recipe.is_file():
            candidates = sorted(rebuild_root.glob("*.dsc"))
            if len(candidates) != 1:
                raise DebianSourceError(
                    f"Expected one rebuilt .dsc, found {len(candidates)}"
                )
            generated_recipe = candidates[0]
        generated_fields = _read_control_fields(generated_recipe)
        if generated_fields.get("Format", "").strip() != SUPPORTED_FORMAT:
            raise DebianSourceError("Rebuilt recipe changed Debian source format")

        referenced_names = _checksum_filenames(generated_recipe)
        for name in referenced_names:
            generated = rebuild_root / name
            destination = input_dir / name
            if ".orig.tar." in name:
                if not destination.is_file() or destination.is_symlink():
                    raise DebianSourceError(f"Original upstream archive is missing: {name}")
                if generated.is_file() and _sha256(generated) != _sha256(destination):
                    raise DebianSourceError(f"Refusing to replace immutable upstream archive: {name}")
                continue
            if not generated.is_file() or generated.is_symlink():
                raise DebianSourceError(f"Rebuilt source component is missing: {name}")
            _copy_atomic(generated, destination)

        if generated_recipe.name != recipe.name:
            raise DebianSourceError(
                f"Rebuilt recipe name changed from {recipe.name} to {generated_recipe.name}"
            )
        _copy_atomic(generated_recipe, recipe)
        return True, None
    finally:
        shutil.rmtree(rebuild_root, ignore_errors=True)


def rebuild_debian_source_if_modified(
    workspace: Path,
    staged_case: Path,
) -> tuple[bool, str | None]:
    """Rebuild a modified materialized source tree only inside staged_case."""

    try:
        return _rebuild(workspace.resolve(), staged_case.resolve())
    except (DebianSourceError, OSError, ValueError) as error:
        return False, f"Debian source rebuild failed: {error}"
