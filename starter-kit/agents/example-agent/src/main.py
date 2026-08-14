from __future__ import annotations

import json
import os
from pathlib import Path


BROKEN_BLOCK = """# BUILD-BENCH-DEMO-BROKEN
echo "intentional Build-Bench demo failure" >&2
exit 1
"""
FIXED_BLOCK = """# BUILD-BENCH-DEMO-REPAIRED
echo "Build-Bench Example Agent repaired the package"
"""


def repair(workspace: Path) -> dict[str, object]:
    initial_log = workspace / "input" / "initial-build.log"
    if "intentional Build-Bench demo failure" not in initial_log.read_text(
        encoding="utf-8", errors="replace"
    ):
        raise RuntimeError("expected demo failure was not found in the build log")

    spec = workspace / "work" / "repo" / "input" / "buildbench-hello.spec"
    content = spec.read_text(encoding="utf-8")
    if content.count(BROKEN_BLOCK) != 1:
        raise RuntimeError("expected exactly one demo failure marker")
    spec.write_text(
        content.replace(BROKEN_BLOCK, FIXED_BLOCK),
        encoding="utf-8",
    )
    return {
        "schema_version": "0.1",
        "status": "completed",
        "message": "Removed the intentional hello build failure.",
        "modified_paths": ["input/buildbench-hello.spec"],
    }


def main() -> int:
    workspace = Path(os.environ.get("BB_WORKSPACE", "/workspace"))
    result = repair(workspace)
    output = workspace / "output" / "agent-result.json"
    output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(result["message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
