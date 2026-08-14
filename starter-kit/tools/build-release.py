#!/usr/bin/env python3
"""Build deterministic, participant-facing Starter Kit release archives."""
from __future__ import annotations
import argparse, gzip, hashlib, io, json, os, stat, tarfile, zipfile
from pathlib import Path, PurePosixPath
ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = ("AGENTS.md", "CLAUDE.md", "bb", "README.md", "VERSION")
REQUIRED_DIRS = ("runner", "templates", "agents/example-agent", "example-cases")
EXCLUDED_NAMES = {"__pycache__", ".DS_Store"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
BANNED_PATH_PARTS = {".git", ".planning", "dist", "docs", "runs", "tests", "tools", "milestone-b-agent"}
BANNED_TEXT = ("milestone a", "milestone b", "milestone c", "/home/zhaochenyu", "10.10.1.226", "buildbench_competition")

def write_text(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle: handle.write(content)

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()

def release_files() -> list[tuple[Path, PurePosixPath]]:
    selected: list[tuple[Path, PurePosixPath]] = []
    for relative in REQUIRED_FILES:
        path = ROOT / relative
        if not path.is_file(): raise FileNotFoundError(f"required release file is missing: {relative}")
        selected.append((path, PurePosixPath(relative)))
    for relative_dir in REQUIRED_DIRS:
        directory = ROOT / relative_dir
        if not directory.is_dir(): raise FileNotFoundError(f"required release directory is missing: {relative_dir}")
        for path in directory.rglob("*"):
            if not path.is_file(): continue
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDED_NAMES for part in relative.parts) or path.suffix in EXCLUDED_SUFFIXES: continue
            selected.append((path, PurePosixPath(relative.as_posix())))
    selected.sort(key=lambda item: item[1].as_posix())
    seen: set[str] = set()
    for _path, relative in selected:
        key = relative.as_posix()
        if key in seen: raise RuntimeError(f"duplicate release path: {key}")
        seen.add(key)
        if set(relative.parts) & BANNED_PATH_PARTS: raise RuntimeError(f"internal path entered release allowlist: {key}")
    return selected

def file_mode(relative: PurePosixPath) -> int:
    return 0o755 if relative.as_posix() in {"bb", "runner/build-case-docker"} or relative.suffix == ".sh" else 0o644

def validate_source(files: list[tuple[Path, PurePosixPath]]) -> None:
    for path, relative in files:
        data = path.read_bytes()
        if b"\x00" in data: continue
        text = data.decode("utf-8", errors="ignore").lower()
        for banned in BANNED_TEXT:
            if banned in text: raise RuntimeError(f"internal marker {banned!r} found in release file {relative.as_posix()}")

def write_zip(path: Path, prefix: str, files: list[tuple[Path, PurePosixPath]]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source, relative in files:
            info = zipfile.ZipInfo(f"{prefix}/{relative.as_posix()}", (1980, 1, 1, 0, 0, 0))
            info.create_system = 3; info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | file_mode(relative)) << 16
            archive.writestr(info, source.read_bytes())
    os.replace(temp_path, path)

def write_tar_gz(path: Path, prefix: str, files: list[tuple[Path, PurePosixPath]]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as archive:
                for source, relative in files:
                    data = source.read_bytes(); info = tarfile.TarInfo(f"{prefix}/{relative.as_posix()}")
                    info.size = len(data); info.mode = file_mode(relative); info.mtime = 0; info.uid = info.gid = 0; info.uname = info.gname = ""
                    archive.addfile(info, io.BytesIO(data))
    os.replace(temp_path, path)

def inspect_archive(zip_path: Path, prefix: str, expected: list[PurePosixPath]) -> None:
    expected_names = sorted(f"{prefix}/{relative.as_posix()}" for relative in expected)
    with zipfile.ZipFile(zip_path) as archive:
        actual_names = sorted(name for name in archive.namelist() if not name.endswith("/"))
        if actual_names != expected_names: raise RuntimeError("release archive contents differ from the strict allowlist")
        for name in actual_names:
            if set(PurePosixPath(name).parts[1:]) & BANNED_PATH_PARTS: raise RuntimeError(f"banned path found: {name}")

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--output-dir", type=Path); args = parser.parse_args()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not version or any(char.isspace() for char in version): raise RuntimeError("VERSION must contain one non-empty token")
    output_dir = (args.output_dir or ROOT.parent / "releases" / "starter-kit" / f"v{version}").resolve(); output_dir.mkdir(parents=True, exist_ok=True)
    files = release_files(); validate_source(files); prefix = f"buildbench-starter-kit-{version}"
    zip_path = output_dir / f"{prefix}.zip"; tar_path = output_dir / f"{prefix}.tar.gz"
    write_zip(zip_path, prefix, files); write_tar_gz(tar_path, prefix, files); inspect_archive(zip_path, prefix, [r for _p, r in files])
    checksums = {zip_path.name: sha256(zip_path), tar_path.name: sha256(tar_path)}
    write_text(output_dir / "SHA256SUMS", "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())))
    manifest = {"schema_version": "1.0", "name": "Build-Bench Starter Kit", "version": version, "archives": [{"file": n, "sha256": d} for n, d in sorted(checksums.items())], "file_count": len(files)}
    write_text(output_dir / "release-manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"Release: {output_dir}"); print(f"Files:   {len(files)}")
    for name, digest in sorted(checksums.items()): print(f"SHA256:  {digest}  {name}")
    return 0
if __name__ == "__main__": raise SystemExit(main())
