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


def main() -> int:
    workspace = Path(os.environ.get("BB_WORKSPACE", "/workspace"))
    initial_log = workspace / "input" / "initial-build.log"
    spec = workspace / "work" / "repo" / "input" / "buildbench-hello.spec"

    if "intentional Build-Bench demo failure" not in initial_log.read_text(
        encoding="utf-8", errors="replace"
    ):
        raise RuntimeError("expected Example Case failure was not found")
    content = spec.read_text(encoding="utf-8")
    if content.count(BROKEN_BLOCK) != 1:
        raise RuntimeError("expected exactly one Example Case failure marker")
    spec.write_text(content.replace(BROKEN_BLOCK, FIXED_BLOCK), encoding="utf-8")

    result = {
        "schema_version": "0.1",
        "status": "completed",
        "message": "Removed the intentional hello build failure.",
        "modified_paths": ["input/buildbench-hello.spec"],
    }
    output = workspace / "output" / "agent-result.json"
    output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(result["message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
