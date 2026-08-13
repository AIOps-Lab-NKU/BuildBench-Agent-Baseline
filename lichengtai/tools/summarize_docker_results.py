#!/usr/bin/env python3
"""Summarize authoritative Docker Validator build-result.json files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_result(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise ValueError("top-level JSON value is not an object")

    return data


def is_verified_success(data: dict[str, Any]) -> bool:
    return (
        data.get("status") == "succeeded"
        and data.get("artifact_validation_passed") is True
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "result_root",
        type=Path,
        help="Run root or docker-results directory",
    )
    args = parser.parse_args()

    root = args.result_root.resolve()

    if not root.is_dir():
        print(f"error=result_root_not_found")
        print(f"result_root={root}")
        return 2

    paths = sorted(
        root.rglob("build-result.json"),
        key=lambda path: (path.stat().st_mtime_ns, str(path)),
    )

    parsed: list[tuple[Path, dict[str, Any]]] = []
    invalid: list[tuple[Path, str]] = []

    for path in paths:
        try:
            parsed.append((path, load_result(path)))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            invalid.append((path, str(exc)))

    verified = [
        (path, data)
        for path, data in parsed
        if is_verified_success(data)
    ]

    print(f"result_root={root}")
    print(f"build_result_count={len(paths)}")
    print(f"parsed_result_count={len(parsed)}")
    print(f"invalid_result_count={len(invalid)}")
    print(f"verified_success_count={len(verified)}")

    if parsed:
        latest_path, latest_data = parsed[-1]
        latest_artifacts = latest_data.get("artifacts") or []

        print(f"latest_result={latest_path}")
        print(f"latest_status={latest_data.get('status')}")
        print(
            "latest_artifact_validation_passed="
            f"{latest_data.get('artifact_validation_passed')}"
        )
        print(
            f"latest_build_exit_code="
            f"{latest_data.get('build_exit_code')}"
        )
        print(f"latest_artifact_count={len(latest_artifacts)}")

    if verified:
        verified_path, verified_data = verified[-1]
        verified_artifacts = verified_data.get("artifacts") or []

        print(f"latest_verified_result={verified_path}")
        print(
            f"latest_verified_duration_seconds="
            f"{verified_data.get('duration_seconds')}"
        )
        print(
            f"latest_verified_artifact_count="
            f"{len(verified_artifacts)}"
        )
        print("final_verdict=verified_build_success")
    else:
        print("latest_verified_result=")
        print("final_verdict=no_verified_success")

    for index, (path, error) in enumerate(invalid, 1):
        safe_error = error.replace("\n", " ").replace("\r", " ")
        print(f"invalid_result_{index}_path={path}")
        print(f"invalid_result_{index}_error={safe_error}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
