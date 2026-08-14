from __future__ import annotations

import argparse
from pathlib import Path

from runner.agent_submission import SubmissionError, initialize_agent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        target = initialize_agent(
            args.template.resolve(),
            (args.target_root / args.name).resolve(),
            args.name,
        )
    except (OSError, UnicodeError, SubmissionError) as error:
        parser.error(str(error))
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
