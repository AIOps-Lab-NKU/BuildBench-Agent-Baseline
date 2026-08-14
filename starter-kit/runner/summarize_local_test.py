from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args()

    cases: list[dict[str, object]] = []
    succeeded = 0
    for case_id in args.case:
        case_dir = args.run_dir / "cases" / case_id
        final = _load(case_dir / "build-result.json")
        initial = _load(case_dir / "initial-build-result.json")
        agent = _load(case_dir / "agent-result.json")
        status = str(final.get("status")) if final else "runner_error"
        if status == "succeeded":
            succeeded += 1
        cases.append(
            {
                "case_id": case_id,
                "status": status,
                "initial_status": initial.get("status") if initial else None,
                "agent_status": agent.get("status") if agent else None,
                "result": str(case_dir / "build-result.json"),
                "log": str(case_dir / "build.log"),
                "patch": str(case_dir / "repair.diff"),
            }
        )

    summary = {
        "schema_version": "0.1",
        "agent_name": args.agent_name,
        "status": "succeeded" if succeeded == len(cases) else "failed",
        "case_count": len(cases),
        "succeeded": succeeded,
        "failed": len(cases) - succeeded,
        "cases": cases,
    }
    output = args.run_dir / "summary.json"
    output.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
