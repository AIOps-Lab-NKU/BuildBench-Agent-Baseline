from __future__ import annotations

import argparse
import json
from pathlib import Path

from runner.agent_submission import SubmissionError, check_submission, write_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--entrypoint", action="store_true")
    args = parser.parse_args()
    try:
        report = check_submission(args.agent)
        if args.report:
            write_report(report, args.report)
    except (OSError, UnicodeError, SubmissionError) as error:
        parser.error(str(error))

    if args.entrypoint:
        for argument in report.entrypoint:
            print(argument)
    elif args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"Agent: {report.agent_name} {report.agent_version}")
        print(f"Entrypoint: {' '.join(report.entrypoint)}")
        print(f"Files: {report.file_count}")
        print(f"Bytes: {report.total_bytes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
