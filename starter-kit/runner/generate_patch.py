from __future__ import annotations

import argparse
import difflib
from pathlib import Path, PurePosixPath


class PatchGenerationError(ValueError):
    pass


def _files(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in {".git", "__pycache__"} for part in relative.parts):
            continue
        found[relative.as_posix()] = path
    return found


def _read_bytes(path: Path | None) -> bytes:
    if path is None:
        return b""
    return path.read_bytes()


def _decode_lines(content: bytes, path: Path | None) -> list[str]:
    if path is None:
        return []
    if b"\0" in content:
        raise PatchGenerationError(f"binary changes are not supported: {path}")
    try:
        return content.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as error:
        raise PatchGenerationError(
            f"changed file is not UTF-8 text: {path}"
        ) from error


def generate_patch(
    original: Path,
    modified: Path,
    output: Path,
    allowed_prefix: str,
) -> tuple[str, ...]:
    original_files = _files(original)
    modified_files = _files(modified)
    paths = sorted(original_files.keys() | modified_files.keys())
    sections: list[str] = []
    changed: list[str] = []

    for relative in paths:
        old_path = original_files.get(relative)
        new_path = modified_files.get(relative)
        old_content = _read_bytes(old_path)
        new_content = _read_bytes(new_path)
        if old_content == new_content:
            continue
        old_lines = _decode_lines(old_content, old_path)
        new_lines = _decode_lines(new_content, new_path)
        normalized = PurePosixPath(relative).as_posix()
        if normalized != relative or not relative.startswith(allowed_prefix):
            raise PatchGenerationError(f"change is outside allowed paths: {relative}")

        changed.append(relative)
        sections.append(f"diff --git a/{relative} b/{relative}\n")
        sections.extend(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{relative}" if old_path else "/dev/null",
                tofile=f"b/{relative}" if new_path else "/dev/null",
                lineterm="\n",
            )
        )

    if not changed:
        raise PatchGenerationError("Agent made no allowed textual change")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(sections), encoding="utf-8")
    return tuple(changed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--modified", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allowed-prefix", default="input/")
    args = parser.parse_args()
    try:
        changed = generate_patch(
            args.original,
            args.modified,
            args.output,
            args.allowed_prefix,
        )
    except (OSError, UnicodeError, PatchGenerationError) as error:
        parser.error(str(error))
    for path in changed:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
