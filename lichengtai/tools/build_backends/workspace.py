"""Prepare the flat workspace expected by the existing Build-Bench Agent."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


WORKSPACE_METADATA = ".buildbench-case.json"


def _copy_contents(source_dir: Path, destination_dir: Path) -> None:
    for source in source_dir.iterdir():
        destination = destination_dir / source.name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)


def prepare_agent_workspace(source_case: Path, destination: Path) -> None:
    """Copy a legacy package or flatten its matching canonical Case leaf.

    A canonical Validator Case contains both ``manifest.json`` and ``input/``.
    Some materialized datasets wrap paired canonical Cases as immediate
    ``source/`` and ``target/`` children.  When the wrapper itself is not
    canonical, select the sole child whose manifest ``case_id`` exactly
    matches the wrapper directory name.

    This keeps selection data-driven and direction-aware without adding
    package-specific or architecture-specific rules.
    """

    source_case = source_case.resolve()

    if destination.exists():
        shutil.rmtree(destination)

    destination.mkdir(parents=True)

    canonical_case: Path | None = None

    manifest = source_case / "manifest.json"
    input_dir = source_case / "input"

    if manifest.is_file() and input_dir.is_dir():
        canonical_case = source_case
    else:
        expected_case_id = source_case.name
        matching_children: list[Path] = []

        for child in sorted(
            source_case.iterdir(),
            key=lambda item: item.name,
        ):
            if (
                not child.is_dir()
                or child.is_symlink()
            ):
                continue

            child_manifest = child / "manifest.json"
            child_input = child / "input"

            if (
                not child_manifest.is_file()
                or not child_input.is_dir()
            ):
                continue

            try:
                metadata = json.loads(
                    child_manifest.read_text(
                        encoding="utf-8",
                    )
                )
            except (
                OSError,
                json.JSONDecodeError,
            ):
                continue

            if not isinstance(metadata, dict):
                continue

            if str(metadata.get("case_id", "")) == expected_case_id:
                matching_children.append(
                    child.resolve()
                )

        if len(matching_children) > 1:
            raise ValueError(
                "Multiple canonical Case children match "
                f"{expected_case_id!r}: "
                + ", ".join(
                    str(path)
                    for path in matching_children
                )
            )

        if len(matching_children) == 1:
            canonical_case = matching_children[0]

    if canonical_case is not None:
        canonical_input = canonical_case / "input"
        _copy_contents(
            canonical_input,
            destination,
        )

        log_candidates = [
            canonical_case
            / "logs"
            / "original-target-failed.log",
            canonical_case
            / "logs"
            / "initial_build.log",
            canonical_case
            / "evidence"
            / "current-failed.log",
            canonical_case
            / "evidence"
            / "original-target-failed.log",
            canonical_case / "failed.log",
        ]

        logs_dir = canonical_case / "logs"

        if logs_dir.is_dir():
            log_candidates.extend(
                sorted(logs_dir.glob("*.log"))
            )

        for log_path in log_candidates:
            if log_path.is_file():
                shutil.copy2(
                    log_path,
                    destination / "log_failed.txt",
                )
                break

        workspace_metadata = {
            "schema_version": "1.0",
            "source_case_dir": str(canonical_case),
        }

        (
            destination / WORKSPACE_METADATA
        ).write_text(
            json.dumps(
                workspace_metadata,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        return

    _copy_contents(
        source_case,
        destination,
    )
