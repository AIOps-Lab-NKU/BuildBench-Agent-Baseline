from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: read_json.py FILE FIELD", file=sys.stderr)
        return 2
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    value = payload
    for component in sys.argv[2].split("."):
        if not isinstance(value, dict) or component not in value:
            print(f"missing JSON field: {sys.argv[2]}", file=sys.stderr)
            return 1
        value = value[component]
    if isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, bool):
        print("true" if value else "false")
    elif value is None:
        print("null")
    else:
        print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

