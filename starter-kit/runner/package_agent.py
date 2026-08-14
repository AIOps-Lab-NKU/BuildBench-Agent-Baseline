from __future__ import annotations

import argparse
from pathlib import Path

from runner.agent_submission import SubmissionError, create_deterministic_zip


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        digest, size = create_deterministic_zip(
            args.agent.resolve(),
            args.output.resolve(),
        )
    except (OSError, UnicodeError, SubmissionError) as error:
        parser.error(str(error))
    print(f"Output: {args.output.resolve()}")
    print(f"Bytes:  {size}")
    print(f"SHA256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
