from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from runner.validate_agent import ManifestError, parse_manifest, validate_manifest


ALLOWED_TOP_LEVEL = {"agent.yaml", "src", "requirements.lock", "README.md"}
REQUIRED_PATHS = {"agent.yaml", "src", "requirements.lock", "README.md"}
IGNORED_GENERATED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
FORBIDDEN_PARTS = {
    ".git",
    ".env",
    "runs",
    "dist",
    "cache",
    ".cache",
}
FORBIDDEN_FILES = {
    "repair.diff",
    "agent-result.json",
    "build-result.json",
    "build.log",
}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
PINNED_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?"
    r"==[A-Za-z0-9_.+!-]+(?:\s*;\s*.+)?$"
)
TEXT_SUFFIXES = {
    "",
    ".py",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".lock",
    ".cfg",
    ".ini",
    ".sh",
}


class SubmissionError(ValueError):
    pass


@dataclass(frozen=True)
class SubmissionReport:
    agent_name: str
    agent_version: str
    entrypoint: tuple[str, ...]
    file_count: int
    total_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "0.1",
            "status": "valid",
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "entrypoint": list(self.entrypoint),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
        }


def _submission_files(agent_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(agent_dir.rglob("*")):
        relative = path.relative_to(agent_dir)
        if any(part in IGNORED_GENERATED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise SubmissionError(f"symbolic links are not allowed: {relative}")
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            raise SubmissionError(f"forbidden path: {relative}")
        if path.name in FORBIDDEN_FILES:
            raise SubmissionError(f"generated or forbidden file: {relative}")
        if path.suffix == ".pyc":
            continue
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise SubmissionError(f"unsupported file type: {relative}")
    return files


def _validate_entrypoint(agent_dir: Path, entrypoint: list[str]) -> None:
    if len(entrypoint) >= 3 and entrypoint[1] == "-m":
        module = entrypoint[2]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module):
            raise SubmissionError(f"invalid Python module entrypoint: {module}")
        module_path = agent_dir.joinpath(*module.split(".")).with_suffix(".py")
        package_path = agent_dir.joinpath(*module.split("."), "__main__.py")
        if not module_path.is_file() and not package_path.is_file():
            raise SubmissionError(
                f"entrypoint module does not exist: {module_path.relative_to(agent_dir)}"
            )
        return

    if len(entrypoint) >= 2 and not entrypoint[1].startswith("-"):
        script = PurePosixPath(entrypoint[1])
        if script.is_absolute() or ".." in script.parts:
            raise SubmissionError("entrypoint script must stay inside the Agent")
        script_path = agent_dir.joinpath(*script.parts)
        if not script_path.is_file():
            raise SubmissionError(f"entrypoint script does not exist: {script}")
        return

    raise SubmissionError(
        "entrypoint must use 'python -m MODULE' or 'python PATH.py'"
    )


def _validate_requirements(path: Path) -> None:
    for number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-", ".", "/", "git+", "http:", "https:")):
            raise SubmissionError(
                f"requirements.lock line {number} uses an unsupported source"
            )
        if not PINNED_REQUIREMENT.fullmatch(line):
            raise SubmissionError(
                f"requirements.lock line {number} must pin one package with =="
            )


def _scan_secrets(files: list[Path], agent_dir: Path) -> None:
    for path in files:
        if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 1_000_000:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                raise SubmissionError(
                    f"possible credential found in {path.relative_to(agent_dir)}"
                )


def check_submission(agent_dir: Path) -> SubmissionReport:
    agent_dir = agent_dir.resolve()
    if not agent_dir.is_dir():
        raise SubmissionError(f"Agent directory does not exist: {agent_dir}")

    top_level = {path.name for path in agent_dir.iterdir()}
    missing = sorted(REQUIRED_PATHS - top_level)
    if missing:
        raise SubmissionError(f"missing required path(s): {', '.join(missing)}")
    unexpected = sorted(top_level - ALLOWED_TOP_LEVEL)
    if unexpected:
        raise SubmissionError(
            f"unexpected top-level path(s): {', '.join(unexpected)}"
        )
    if not (agent_dir / "src").is_dir():
        raise SubmissionError("src must be a directory")
    for filename in ("agent.yaml", "requirements.lock", "README.md"):
        if not (agent_dir / filename).is_file():
            raise SubmissionError(f"{filename} must be a regular file")

    try:
        data = parse_manifest(agent_dir / "agent.yaml")
        entrypoint = validate_manifest(data)
    except ManifestError as error:
        raise SubmissionError(str(error)) from error
    _validate_entrypoint(agent_dir, entrypoint)
    _validate_requirements(agent_dir / "requirements.lock")

    files = _submission_files(agent_dir)
    if not any(path.suffix == ".py" for path in files if "src" in path.parts):
        raise SubmissionError("src must contain at least one Python file")
    _scan_secrets(files, agent_dir)

    agent = data["agent"]
    assert isinstance(agent, dict)
    return SubmissionReport(
        agent_name=str(agent["name"]),
        agent_version=str(agent["version"]),
        entrypoint=tuple(entrypoint),
        file_count=len(files),
        total_bytes=sum(path.stat().st_size for path in files),
    )


def initialize_agent(template_dir: Path, target_dir: Path, name: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", name):
        raise SubmissionError(
            "NAME must contain 2-64 lowercase letters, digits, or hyphens"
        )
    if target_dir.exists():
        raise SubmissionError(f"target already exists: {target_dir}")
    if not template_dir.is_dir():
        raise SubmissionError(f"Agent template is missing: {template_dir}")

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template_dir, target_dir)
    manifest = target_dir / "agent.yaml"
    content = manifest.read_text(encoding="utf-8")
    if "__AGENT_NAME__" not in content:
        shutil.rmtree(target_dir)
        raise SubmissionError("Agent template name placeholder is missing")
    manifest.write_text(
        content.replace("__AGENT_NAME__", name),
        encoding="utf-8",
    )
    return target_dir


def create_deterministic_zip(agent_dir: Path, output: Path) -> tuple[str, int]:
    check_submission(agent_dir)
    files = _submission_files(agent_dir.resolve())
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()

    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in files:
                relative = path.relative_to(agent_dir.resolve()).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (
                    stat.S_IFREG | (0o755 if path.suffix == ".sh" else 0o644)
                ) << 16
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return digest, output.stat().st_size


def write_report(report: SubmissionReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
