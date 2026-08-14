from __future__ import annotations

import argparse
import re
from pathlib import Path


class ManifestError(ValueError):
    pass


_SCALAR = re.compile(r'^([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]+(.*))?$')


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_manifest(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ManifestError("agent.yaml is missing")
    root: dict[str, object] = {}
    section: str | None = None
    list_key: str | None = None

    for number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if "\t" in raw_line:
            raise ManifestError(f"tabs are not allowed (line {number})")
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        text = raw_line.strip()

        if indent == 0:
            match = _SCALAR.fullmatch(text)
            if match is None:
                raise ManifestError(f"invalid top-level field (line {number})")
            key, raw_value = match.groups()
            if key in root:
                raise ManifestError(f"duplicate field: {key}")
            if raw_value is None:
                root[key] = {}
                section = key
                list_key = None
            else:
                root[key] = _unquote(raw_value)
                section = None
                list_key = None
            continue

        if indent == 2 and text.startswith("- "):
            if section is None or list_key != section:
                if section is None or root.get(section) != {}:
                    raise ManifestError(f"list item has no list field (line {number})")
                root[section] = []
                list_key = section
            values = root[section]
            assert isinstance(values, list)
            values.append(_unquote(text[2:]))
            continue

        if indent == 2 and section is not None:
            match = _SCALAR.fullmatch(text)
            if match is None or match.group(2) is None:
                raise ManifestError(f"invalid nested field (line {number})")
            values = root[section]
            if not isinstance(values, dict):
                raise ManifestError(f"mapping/list conflict (line {number})")
            key, raw_value = match.groups()
            if key in values:
                raise ManifestError(f"duplicate field: {section}.{key}")
            values[key] = _unquote(raw_value)
            continue

        raise ManifestError(f"unsupported indentation (line {number})")

    return root


def validate_manifest(data: dict[str, object]) -> list[str]:
    if data.get("schema_version") != "0.1":
        raise ManifestError('schema_version must be "0.1"')

    agent = data.get("agent")
    if not isinstance(agent, dict):
        raise ManifestError("agent mapping is required")
    for field in ("name", "version"):
        if not isinstance(agent.get(field), str) or not agent[field]:
            raise ManifestError(f"agent.{field} is required")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", str(agent["name"])):
        raise ManifestError(
            "agent.name must contain 2-64 lowercase letters, digits, or hyphens"
        )

    runtime = data.get("runtime")
    if runtime != {"type": "managed", "profile": "python-3.11"}:
        raise ManifestError("v0.1 requires managed python-3.11 runtime")

    entrypoint = data.get("entrypoint")
    if (
        not isinstance(entrypoint, list)
        or not entrypoint
        or not all(isinstance(item, str) and item for item in entrypoint)
    ):
        raise ManifestError("entrypoint must be a non-empty string list")
    if entrypoint[0] not in {"python", "python3"}:
        raise ManifestError("managed runtime entrypoint must start with python")

    protocol = data.get("protocol")
    if not isinstance(protocol, dict) or protocol.get("version") != "0.1":
        raise ManifestError('protocol.version must be "0.1"')
    return entrypoint


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--entrypoint", action="store_true")
    parser.add_argument("--agent-name", action="store_true")
    parser.add_argument("--agent-version", action="store_true")
    args = parser.parse_args()
    try:
        data = parse_manifest(args.manifest)
        entrypoint = validate_manifest(data)
    except (OSError, UnicodeError, ManifestError) as error:
        parser.error(str(error))
    if args.entrypoint:
        for argument in entrypoint:
            print(argument)
    if args.agent_name:
        print(data["agent"]["name"])
    if args.agent_version:
        print(data["agent"]["version"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
