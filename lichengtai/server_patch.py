import os
import json
import shutil
import difflib
import time
import re
import subprocess
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from tools.auto_repair.get_repo_structure import get_project_structure_from_local
from tools.auto_repair.check_build_res import check_main
from tools.auto_repair.upload_files import main_upload
from tools.build_backends.service import run_build_validation
from tools.build_backends.debian_source import prepare_debian_source_workspace
from tools.build_backends.rpm_source import prepare_rpm_source_workspace
from tools.build_backends.workspace import prepare_agent_workspace
import tarfile
import zipfile
from typing import List, Tuple

mcp = FastMCP("auto_repair_server_patch")

# Server-side state storage
server_state = {
    "modification_history": {},  # {package_name: [{file_path, diff, timestamp}, ...]}
    "tool_call_history": {},  # {package_name: [(tool_name, args_key), ...]}
    "tool_cache": {},  # {package_name: {call_key: result, ...}}
    # Trusted server-side mappings:
    # resolved agent workspace -> resolved original Case directory.
    "workspace_sources": {},
    # resolved agent workspace -> resolved active editable source root.
    "workspace_edit_roots": {},
}


def _resolve_normal_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def _path_is_within(root: str, candidate: str) -> bool:
    try:
        return os.path.commonpath([root, candidate]) == root
    except ValueError:
        return False


def _registered_workspace_for_path(path: str):
    candidate = _resolve_normal_path(path)
    workspace_sources = server_state.get("workspace_sources", {})

    matches = [
        workspace
        for workspace in workspace_sources
        if _path_is_within(workspace, candidate)
    ]

    if not matches:
        return None, None, candidate

    workspace = max(matches, key=len)
    active_root = server_state.get(
        "workspace_edit_roots", {}
    ).get(workspace, workspace)
    active_root = _resolve_normal_path(active_root)

    return workspace, active_root, candidate



def _authorize_read_path(path: str):
    """Authorize reads anywhere inside a registered Agent workspace."""
    workspace, active_root, candidate = (
        _registered_workspace_for_path(path)
    )

    if workspace is None:
        return (
            None,
            None,
            candidate,
            "path is outside every registered Agent workspace",
        )

    return workspace, active_root, candidate, None


def _authorize_active_path(path: str):
    workspace, active_root, candidate = (
        _registered_workspace_for_path(path)
    )

    if workspace is None:
        return (
            None,
            None,
            candidate,
            "path is outside every registered Agent workspace",
        )

    if not _path_is_within(active_root, candidate):
        return (
            workspace,
            active_root,
            candidate,
            "path is outside the active edit root: "
            f"{active_root}",
        )

    return workspace, active_root, candidate, None


def _authorize_edit_root(repo_root: str):
    workspace, active_root, candidate = (
        _registered_workspace_for_path(repo_root)
    )

    if workspace is None:
        return (
            None,
            None,
            "repo_root is outside every registered Agent workspace",
        )

    if candidate != active_root:
        return (
            workspace,
            active_root,
            "repo_root must equal the active edit root: "
            f"{active_root}",
        )

    return workspace, active_root, None


@mcp.tool()
def init_package_environment_tool(
    base_dir: str, package_name: str, temp_work_dir: str, result_dir: str
) -> str:
    """
    Initializes the package's temporary working environment and copies the original files to a temporary directory.
    Args:
        base_dir: Base directory
        package_name: Package name
        temp_work_dir: Temporary working directory
        result_dir: Result storage directory
    Returns:
        JSON string containing the initialization result
    """
    try:
        package_temp_dir = os.path.join(temp_work_dir, package_name)
        original_package_path = os.path.join(base_dir, package_name)
        if not os.path.exists(original_package_path):
            return json.dumps(
                {
                    "success": False,
                    "message": f"Original package path not found: {original_package_path}",
                }
            )

        prepare_agent_workspace(
            Path(original_package_path), Path(package_temp_dir)
        )

        workspace_real = os.path.realpath(os.path.abspath(package_temp_dir))
        source_case_real = os.path.realpath(
            os.path.abspath(original_package_path)
        )
        server_state.setdefault("workspace_sources", {})[
            workspace_real
        ] = source_case_real
        server_state.setdefault("workspace_edit_roots", {})[
            workspace_real
        ] = workspace_real

        result_file = os.path.join(result_dir, f"{package_name}_result.txt")

        return json.dumps(
            {
                "success": True,
                "package_temp_dir": package_temp_dir,
                "package_path": package_temp_dir,
                "result_file": result_file,
                "message": f"Initialized package environment: {package_temp_dir}",
            }
        )
    except Exception as e:
        return json.dumps(
            {"success": False, "message": f"Initialization failed: {str(e)}"}
        )


@mcp.tool()
def track_file_modification_tool(
    package_name: str,
    file_path: str,
    package_path: str,
    old_content: str,
    new_content: str,
) -> str:
    """
    Tracks file modification history and records differences.
    Args:
        package_name: Package name
        file_path: File path (relative path)
        package_path: Package path
        old_content: Content before modification
        new_content: Content after modification
    Returns:
        Tracking results
    """
    try:
        if package_name not in server_state["modification_history"]:
            server_state["modification_history"][package_name] = []

        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)

        # Calculate the difference
        diff = []
        for i, line in enumerate(
            difflib.unified_diff(old_lines, new_lines, lineterm="")
        ):
            if i < 3:
                continue
            if line.startswith(""):
                diff.append(
                    {
                        "operation": "add",
                        "line_number": i - 2,
                        "content": line[1:].rstrip("\n"),
                    }
                )
            elif line.startswith("-"):
                diff.append(
                    {
                        "operation": "delete",
                        "line_number": i - 2,
                        "content": line[1:].rstrip("\n"),
                    }
                )
            elif line.startswith(" "):
                diff.append(
                    {
                        "operation": "keep",
                        "line_number": i - 2,
                        "content": line[1:].rstrip("\n"),
                    }
                )

        # Storage differences
        server_state["modification_history"][package_name].append(
            {
                "file_path": file_path,
                "diff": diff,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

        return f"Successfully tracked modification to {file_path}"
    except Exception as e:
        return f"Error tracking modification: {str(e)}"


def _parse_build_result_payload(
    result_content: str,
) -> dict:
    """Parse bounded structured build outcome fields."""
    try:
        structured = json.loads(result_content)
    except json.JSONDecodeError:
        structured = None

    if isinstance(structured, dict) and "success" in structured:
        evidence = structured.get(
            "outcome_evidence"
        )

        if not isinstance(evidence, dict):
            evidence = {}

        return {
            "success": bool(structured["success"]),
            "status": structured.get(
                "status",
                "unknown",
            ),
            "outcome_evidence": evidence,
        }

    status = result_content.split(": ", 1)[-1]
    low = status.lower()
    success = any(
        keyword in low
        for keyword in [
            "success",
            "succeeded",
            "successfully",
            "passed",
            "ok",
        ]
    )

    return {
        "success": success,
        "status": status,
        "outcome_evidence": {},
    }


@mcp.tool()
def parse_build_result_tool(
    result_content: str,
    package_name: str,
) -> str:
    """Parse a build result while preserving bounded outcome evidence."""
    try:
        return json.dumps(
            _parse_build_result_payload(
                result_content
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception:
        return json.dumps(
            {
                "success": False,
                "status": "Unknown (parse error)",
                "outcome_evidence": {},
            },
            ensure_ascii=False,
            sort_keys=True,
        )


@mcp.tool()
def update_prompt_with_history_tool(
    package_name: str, package_path: str, build_attempt: int, formatted_prompt: str
) -> str:
    """
    Update prompt, including modification history.
    Args:
        package_name: Package name
        package_path: Package path
        build_attempt: Number of build attempts
        formatted_prompt: Formatted system prompt
    Returns:
        JSON string containing the updated message list
    """
    prompt_parts = [
        f"Please analyze and repair package {package_name} in: {package_path}.",
        "All modifications must be done in the temporary directory.",
    ]

    if build_attempt > 1:
        prompt_parts.append(
            f"After {build_attempt - 1} attempts, the build still failed."
        )

        prev_modifications = server_state["modification_history"].get(
            package_name, []
        )

        if prev_modifications:
            prompt_parts.append("Previous modifications:")

            for modification in prev_modifications:
                file_path = modification.get("file_path", "<unknown>")
                prompt_parts.append(f"File: {file_path}")

                changes = modification.get("diff")
                if isinstance(changes, list):
                    for change in changes:
                        if not isinstance(change, dict):
                            continue

                        operation = change.get("operation", "modify")
                        line_number = change.get("line_number", "?")
                        content = str(change.get("content", ""))[:200]

                        prompt_parts.append(
                            f"- Line {line_number} "
                            f"({operation}): {content}"
                        )
                else:
                    operation = modification.get(
                        "operation", "modified"
                    )
                    prompt_parts.append(f"- Operation: {operation}")

        prompt_parts.append(
            "Analyze previous modifications and failures, "
            "then provide a new repair plan."
        )

    current_prompt = "\n".join(prompt_parts)

    return json.dumps(
        {
            "messages": [
                {"role": "system", "content": formatted_prompt},
                {"role": "user", "content": current_prompt},
            ]
        }
    )


@mcp.tool()
def get_file_content_tool(file_path: str) -> str:
    """
    Read one file anywhere inside its registered Agent workspace.

    Editing remains restricted to the active edit root.
    """
    try:
        _, _, authorized_path, authorization_error = (
            _authorize_read_path(file_path)
        )

        if authorization_error is not None:
            return f"Error: {authorization_error}"

        if not os.path.isfile(authorized_path):
            return f"Error: File not found - {file_path}"

        with open(authorized_path, "r", encoding="utf-8") as handle:
            return handle.read()
    except Exception as error:
        return f"Error reading file: {str(error)}"


@mcp.tool()
def check_repeat_tool_call(
    tool_name: str, args_key: str, max_repeat: int, package_name: str
) -> str:
    """
    Check if the tool call is repeated
    Args:
        tool_name: Tool name
        args_key: Argument key
        max_repeat: Maximum repeat count
        package_name: Package name
    Returns:
        JSON string containing the check results
    """
    call_key = args_key
    if package_name not in server_state["tool_call_history"]:
        server_state["tool_call_history"][package_name] = []

    repeat_count = server_state["tool_call_history"][package_name].count(call_key)
    if repeat_count >= max_repeat:
        return json.dumps(
            {
                "allowed": False,
                "message": f"Tool call {tool_name} exceeded max repeat count ({max_repeat})",
            }
        )
    return json.dumps({"allowed": True, "message": "Tool call allowed"})


@mcp.tool()
def check_tool_cache(call_key: str, tool_name: str, package_name: str) -> str:
    """
    Check tool call result cache
    Args:
        call_key: Call key
        tool_name: Tool name
        package_name: Package name
    Returns:
        JSON string containing the cache check results
    """
    if package_name not in server_state["tool_cache"]:
        server_state["tool_cache"][package_name] = {}

    if call_key in server_state["tool_cache"][package_name]:
        return json.dumps(
            {"hit": True, "result": server_state["tool_cache"][package_name][call_key]}
        )
    return json.dumps({"hit": False, "result": ""})


@mcp.tool()
def cache_tool_result(call_key: str, result: str, package_name: str) -> str:
    """
    Cache tool call result
    Args:
        call_key: Call key
        result: Result content
        package_name: Package name
    Returns:
        Cache result
    """
    if package_name not in server_state["tool_cache"]:
        server_state["tool_cache"][package_name] = {}
    server_state["tool_cache"][package_name][call_key] = result
    return f"Successfully cached result for {call_key}"


@mcp.tool()
def reset_package_cache_tool(package_name: str) -> str:
    """
    Clear per-package caches for a new attempt.
    - Clears: tool_cache and tool_call_history
    - Keeps:  modification_history (so dynamic prompt can use it)
    """
    # initialize keys if missing
    server_state.setdefault("tool_cache", {})
    server_state.setdefault("tool_call_history", {})
    server_state.setdefault("modification_history", {})

    # clear cache  call history for this package
    if package_name in server_state["tool_cache"]:
        server_state["tool_cache"][package_name].clear()
    if package_name in server_state["tool_call_history"]:
        server_state["tool_call_history"][package_name].clear()

    return json.dumps(
        {
            "success": True,
            "message": f"Cleared tool_cache and tool_call_history for package '{package_name}'.",
        }
    )


@mcp.tool()
def record_tool_call_history(call_key: str, package_name: str) -> str:
    """
    Record tool call history
    Args:
        call_key: Call key
        package_name: Package name
    Returns:
        Record result
    """
    if package_name not in server_state["tool_call_history"]:
        server_state["tool_call_history"][package_name] = []
    server_state["tool_call_history"][package_name].append(call_key)
    return f"Recorded tool call history for {package_name}"


@mcp.tool()
def get_structure_of_files(package_path) -> dict:
    """
    Retrieve structure anywhere inside the registered Agent workspace.

    This does not broaden the writable patch root.
    """
    _, _, authorized_path, authorization_error = (
        _authorize_read_path(package_path)
    )

    if authorization_error is not None:
        return {"success": False, "error": authorization_error}

    if not os.path.isdir(authorized_path):
        return {
            "success": False,
            "error": f"Directory not found: {package_path}",
        }

    return get_project_structure_from_local(authorized_path)


@mcp.tool()
def query_dependency_api_tool(
    package_path: str,
    package_fragment: str,
    symbol_pattern: str,
) -> str:
    """
    Query symbols exposed by an RPM from the current Case's frozen
    dependencies.

    The original Case directory is resolved from trusted server-side state.
    The caller cannot supply an arbitrary dependency directory.

    Args:
        package_path: Initialized Agent workspace path.
        package_fragment: Literal fragment used to select an RPM package.
        symbol_pattern: Restricted regular expression matched against
            identifier-like strings found in interface files.

    Returns:
        A bounded JSON result containing package metadata and matching symbols.
    """
    try:
        workspace_real = os.path.realpath(os.path.abspath(package_path))

        source_case = server_state.get("workspace_sources", {}).get(
            workspace_real
        )
        if not source_case:
            return json.dumps(
                {
                    "success": False,
                    "error": "Unregistered Agent workspace",
                }
            )

        if not re.fullmatch(
            r"[A-Za-z0-9._+-]{1,80}",
            package_fragment or "",
        ):
            return json.dumps(
                {
                    "success": False,
                    "error": "Invalid package_fragment",
                }
            )

        if (
            not symbol_pattern
            or len(symbol_pattern) > 120
            or any(ch in symbol_pattern for ch in "\r\n\0")
        ):
            return json.dumps(
                {
                    "success": False,
                    "error": "Invalid symbol_pattern",
                }
            )

        try:
            re.compile(symbol_pattern)
        except re.error as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Invalid regular expression: {exc}",
                }
            )

        rpm_dir = Path(source_case) / "dependencies" / "rpms"
        if not rpm_dir.is_dir():
            return json.dumps(
                {
                    "success": False,
                    "error": "Frozen RPM dependency directory not found",
                }
            )

        candidates = sorted(
            path
            for path in rpm_dir.iterdir()
            if (
                path.is_file()
                and path.name.endswith(".rpm")
                and package_fragment.lower() in path.name.lower()
            )
        )

        if not candidates:
            return json.dumps(
                {
                    "success": False,
                    "error": "No matching RPM dependency",
                }
            )

        if len(candidates) > 20:
            return json.dumps(
                {
                    "success": False,
                    "error": "Too many matching RPM dependencies",
                    "candidate_count": len(candidates),
                }
            )

        selected = next(
            (
                path
                for path in candidates
                if "-devel-" in path.name
            ),
            candidates[0],
        )

        rpm_size = selected.stat().st_size
        if rpm_size > 256 * 1024 * 1024:
            return json.dumps(
                {
                    "success": False,
                    "error": "Selected RPM exceeds size limit",
                    "rpm_bytes": rpm_size,
                }
            )

        image = os.environ.get(
            "BUILD_BENCH_DEP_API_IMAGE",
            "buildbench-validator-runtime:v0",
        )

        container_script = r"""
set -eu

ROOT=/tmp/pkg
mkdir -p "$ROOT"
bsdtar -xf /input.rpm -C "$ROOT"

echo "[package]"
find "$ROOT" -type f -name "*.conf" \
  -exec grep -E \
    "^(name|version|exposed-modules|depends):" {} \; \
  2>/dev/null \
  | sed -n "1,40p" \
  || true

echo "[interface_files]"
find "$ROOT" -type f \
  \( \
    -name "*.hi" \
    -o -name "*.dyn_hi" \
    -o -name "*.hie" \
    -o -name "*.conf" \
    -o -name "*.haddock" \
    -o -name "*.hs" \
  \) \
  -printf "%P\n" \
  | sort \
  | sed -n "1,80p"

echo "[matching_symbols]"
find "$ROOT" -type f \
  \( \
    -name "*.hi" \
    -o -name "*.dyn_hi" \
    -o -name "*.hie" \
    -o -name "*.conf" \
    -o -name "*.haddock" \
    -o -name "*.hs" \
  \) \
  -exec strings -a {} + 2>/dev/null \
  | grep -Eo "[A-Za-z_][A-Za-z0-9_]{2,127}" \
  | grep -E -- "$QUERY_REGEX" \
  | sort -u \
  | sed -n "1,100p" \
  || true
"""

        container_name = (
            f"bb-dep-api-{os.getpid()}-{time.time_ns()}"
        )

        command = [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--memory",
            "512m",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=384m",
            "-e",
            f"QUERY_REGEX={symbol_pattern}",
            "-v",
            f"{selected}:/input.rpm:ro",
            "--entrypoint",
            "/bin/sh",
            image,
            "-c",
            container_script,
        ]

        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            cleanup = subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        "Dependency API query timed out after "
                        "30 seconds"
                    ),
                    "container_cleanup_exit_code": cleanup.returncode,
                }
            )

        stdout = completed.stdout[:16000]
        stderr = completed.stderr[:2000]

        if completed.returncode != 0:
            return json.dumps(
                {
                    "success": False,
                    "error": "Dependency API query container failed",
                    "exit_code": completed.returncode,
                    "stderr": stderr,
                }
            )

        return json.dumps(
            {
                "success": True,
                "source": "frozen_case_dependency",
                "selected_rpm": selected.name,
                "rpm_bytes": rpm_size,
                "candidate_count": len(candidates),
                "query": symbol_pattern,
                "result": stdout,
            },
            ensure_ascii=False,
        )

    except subprocess.TimeoutExpired:
        return json.dumps(
            {
                "success": False,
                "error": "Dependency API query timed out after 30 seconds",
            }
        )
    except Exception as exc:
        return json.dumps(
            {
                "success": False,
                "error": f"Dependency API query failed: {exc}",
            }
        )


def _parse_git_unified_patch(patch_text: str):
    """
    Parse unified diff (supports multi-file).
    Returns: List[{"old":old_path,"new":new_path,"hunks":[(a_l,a_s,b_l,b_s,lines)],"is_new":bool,"is_delete":bool}]
    where lines are original '+/-/ ' lines (no newline), maintaining order.
    """
    lines = patch_text.splitlines()
    i, n = 0, len(lines)
    files = []

    def read_file_block(start):
        """Read a single file block from start index
        Hope start is at 'diff --git a/foo b/foo' line.
        @@ -l,s +l,s @@
        """
        old_path = new_path = None
        is_new = is_delete = False
        hunks = []

        # skip until see --- / +++
        while start < n and not lines[start].startswith("--- "):
            start += 1
        if start >= n:
            return start, None

        # line ---
        m = re.match(r"^---\s+(.*)$", lines[start])
        start += 1
        old_tok = m.group(1).strip() if m else None

        # line +++
        if start >= n or not lines[start].startswith("+++ "):
            raise ValueError("Bad patch: expected '+++' after '---'")
        m = re.match(r"^\+\+\+\s+(.*)$", lines[start])
        start += 1
        new_tok = m.group(1).strip() if m else None

        if old_tok == "/dev/null":
            is_new = True
            old_path = None
        else:
            old_path = old_tok[2:] if old_tok.startswith("a/") else old_tok

        if new_tok == "/dev/null":
            is_delete = True
            new_path = None
        else:
            new_path = new_tok[2:] if new_tok.startswith("b/") else new_tok

        # read hunks
        while start < n and lines[start].startswith("@@"):
            hdr = lines[start]
            start += 1
            m = re.match(
                r"^@@\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@.*$",
                hdr.rstrip("\r"),
            )
            if not m:
                raise ValueError(f"Bad hunk header: {hdr}")
            a_l = int(m.group(1))
            a_s = int(m.group(2) or 1)
            b_l = int(m.group(3))
            b_s = int(m.group(4) or 1)
            body = []
            while (
                start < n
                and not lines[start].startswith("@@")
                and not lines[start].startswith("diff --git")
                and not lines[start].startswith("--- ")
            ):
                body.append(lines[start])
                start += 1
            hunks.append((a_l, a_s, b_l, b_s, body))

        return start, {
            "old": old_path,
            "new": new_path,
            "hunks": hunks,
            "is_new": is_new,
            "is_delete": is_delete,
        }

    while i < n:
        if lines[i].startswith("diff --git "):
            i += 1
            # --- / +++ / @@
            i, f = read_file_block(i)
            if f:
                files.append(f)
        elif lines[i].startswith("--- "):
            i, f = read_file_block(i)
            if f:
                files.append(f)
        else:
            i += 1
    return files


def _apply_hunks_strict(
    orig_lines: List[str], hunks: List[Tuple[int, int, int, int, List[str]]]
) -> Tuple[List[str], bool]:
    """
    Strictly apply hunks (no fuzzy matching). orig_lines/returned lines contain newline characters.
    """
    new_lines = orig_lines[:]
    line_offset = 0
    for a_l, a_s, b_l, b_s, body in hunks:
        start = a_l - 1 + line_offset
        cur = start
        replacement = []
        for raw in body:
            if raw.startswith("+"):
                replacement.append(raw[1:] + "\n")
            elif raw.startswith(" "):
                if cur >= len(new_lines):
                    return orig_lines, False
                if new_lines[cur].rstrip("\n") != raw[1:]:
                    return orig_lines, False
                replacement.append(new_lines[cur])
                cur += 1
            elif raw.startswith("-"):
                if cur >= len(new_lines):
                    return orig_lines, False
                if new_lines[cur].rstrip("\n") != raw[1:]:
                    return orig_lines, False
                cur += 1
            else:
                if cur >= len(new_lines):
                    return orig_lines, False
                replacement.append(new_lines[cur])
                cur += 1
        before_len = len(new_lines)
        new_lines[start:cur] = replacement
        after_len = len(new_lines)
        line_offset += (after_len - before_len) - (cur - start)
    return new_lines, True


@mcp.tool()
def apply_git_unified_patch_tool(
    repo_root: str,
    patch_text: str = "",
    record_history: bool = True,
    mode: str = "unified_diff",
    file_path: str = "",
    old_text: str = "",
    new_text: str = "",
    expected_count: int = 1,
) -> str:
    """
    Apply GitHub-style unified diff (multi-file, supports new/delete).
    - repo_root: Repository root directory (e.g., {temp_dir})
    - patch_text: Full patch text (includes diff --git / --- / +++ / @@)

    Modes:
    - mode="unified_diff": Apply patch_text using strict unified-diff matching.
    - mode="exact_replace": Replace old_text with new_text in file_path only
      when the number of exact matches equals expected_count.

    For exact_replace:
    - file_path must be relative to repo_root.
    - old_text must be non-empty.
    - expected_count must be a positive integer.
    - The file is not modified when the match count differs.

    Notes:
    1. **Strict Hunk Matching**: No fuzzy apply—context lines in patch must exactly match target file.
       - Fix "Hunk failed": Check context lines ( ' ' prefix) for typos/line mismatches, or add more context (3-5 lines).
    2. **Valid Hunk Header**: Must follow `@@ -<start>[,<len>] +<start>[,<len>] @@` format.
       - Fix "Bad hunk header": Never use empty `@@`; specify line ranges (e.g., `@@ -41,11 +41,16 @@`).
    3. **Path Check**: Ensure patch paths are relative to repo_root (no absolute paths).
    """
    try:
        root = os.path.realpath(os.path.abspath(repo_root))
        if not os.path.isdir(root):
            return f"Error: repo_root not found: {repo_root}"

        workspace_root, active_root, authorization_error = (
            _authorize_edit_root(root)
        )
        if authorization_error is not None:
            return f"Error: {authorization_error}"

        history_key = os.path.basename(
            workspace_root.rstrip(os.sep)
        )

        if mode == "exact_replace":
            if not file_path:
                return "Error: exact_replace requires file_path"
            if os.path.isabs(file_path):
                return "Error: exact_replace file_path must be relative to repo_root"
            if not old_text:
                return "Error: exact_replace requires non-empty old_text"
            if old_text == new_text:
                return "Error: exact_replace old_text and new_text are identical"

            try:
                required_count = int(expected_count)
            except (TypeError, ValueError):
                return "Error: exact_replace expected_count must be an integer"

            if required_count < 1:
                return "Error: exact_replace expected_count must be positive"

            target_rel = os.path.normpath(file_path)
            target_abs = os.path.realpath(os.path.join(root, target_rel))

            try:
                inside_root = os.path.commonpath([root, target_abs]) == root
            except ValueError:
                inside_root = False

            if not inside_root or target_abs == root:
                return f"Error: path escapes repo_root: {file_path}"

            if not os.path.isfile(target_abs):
                return f"Error: target not found for exact_replace: {target_rel}"

            with open(target_abs, "r", encoding="utf-8") as fr:
                original_content = fr.read()

            actual_count = original_content.count(old_text)
            if actual_count != required_count:
                return (
                    "Error: exact_replace match count mismatch for "
                    f"{target_rel}; expected_count={required_count}; "
                    f"actual_count={actual_count}; file unchanged"
                )

            replaced_content = original_content.replace(
                old_text,
                new_text,
                required_count,
            )

            with open(target_abs, "w", encoding="utf-8") as fw:
                fw.write(replaced_content)

            if record_history:
                server_state.setdefault("modification_history", {})
                server_state["modification_history"].setdefault(
                    history_key, []
                ).append(
                    {
                        "file_path": target_rel,
                        "operation": "exact_replace",
                        "replacement_count": actual_count,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )

            return (
                "Success: exact_replace\n"
                f"M {target_rel}\n"
                f"replacements={actual_count}"
            )

        if mode != "unified_diff":
            return (
                f"Error: unsupported mode {mode!r}; "
                "expected 'unified_diff' or 'exact_replace'"
            )

        files = _parse_git_unified_patch(patch_text)
        if not files:
            head = "\n".join(patch_text.splitlines()[:8])
            return "Error: No file blocks detected in patch.\nHead:\n" + head

        applied = []
        for f in files:
            old_p = f["old"]
            new_p = f["new"]
            is_new = f["is_new"]
            is_delete = f["is_delete"]
            target_rel = new_p if not is_delete else old_p
            if not target_rel:
                return "Error: invalid file block (both paths /dev/null?)"

            target_abs = os.path.realpath(
                os.path.join(root, target_rel)
            )
            if not _path_is_within(root, target_abs) or target_abs == root:
                return f"Error: path escapes repo_root: {target_rel}"

            # Read original content
            if is_new:
                orig = ""
            else:
                if not os.path.exists(target_abs):
                    return f"Error: target not found for patch: {target_rel}"
                with open(target_abs, "r", encoding="utf-8") as fr:
                    orig = fr.read()
            orig_lines = orig.splitlines(keepends=True)

            # Apply/Delete
            if is_delete:
                # Delete file
                os.remove(target_abs)
                new_lines = []
                ok = True
            else:
                new_lines, ok = _apply_hunks_strict(orig_lines, f["hunks"])

            if not ok:
                return f"Error: Hunk failed for {target_rel}. Ensure exact context lines and correct ranges."

            # Write back new/modified content
            if not is_delete:
                os.makedirs(os.path.dirname(target_abs), exist_ok=True)
                with open(target_abs, "w", encoding="utf-8") as fw:
                    fw.write("".join(new_lines))

            applied.append(
                f"{'A' if is_new else ('D' if is_delete else 'M')} {target_rel}"
            )

            # Record history
            if record_history:
                server_state.setdefault("modification_history", {})
                server_state["modification_history"].setdefault(
                    history_key, []
                ).append(
                    {
                        "file_path": target_rel,
                        "operation": "git_unified_patch",
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )

        return "Success: applied patch\n" + "\n".join(applied)
    except Exception as e:
        return f"Error: {str(e)}"


SUPPORTED_FORMATS = {
    "tar.gz": {"extensions": [".tar.gz", ".tgz"], "mode": "r:gz"},
    "tar.xz": {"extensions": [".tar.xz", ".txz"], "mode": "r:xz"},
    "tar.bz2": {"extensions": [".tar.bz2", ".tbz"], "mode": "r:bz2"},
    "zip": {"extensions": [".zip"], "mode": "r"},
}


def get_archive_format(file_path: str) -> tuple:
    """Determine archive format"""
    for fmt, info in SUPPORTED_FORMATS.items():
        for ext in info["extensions"]:
            if file_path.lower().endswith(ext):
                return (fmt, info["mode"])
    return (None, None)


@mcp.tool()
def prepare_debian_source_tool(package_path: str) -> str:
    """Materialize and register the sole Debian editable source root."""
    workspace_real = _resolve_normal_path(package_path)

    if workspace_real not in server_state.get(
        "workspace_sources", {}
    ):
        return json.dumps(
            {
                "success": False,
                "status": "unregistered_workspace",
                "message": (
                    "prepare_debian_source_tool requires the "
                    "registered package workspace"
                ),
            },
            ensure_ascii=False,
        )

    result = prepare_debian_source_workspace(workspace_real)

    if not isinstance(result, dict):
        return json.dumps(
            {
                "success": False,
                "status": "invalid_prepare_result",
                "message": "Debian source preparation returned no object",
            },
            ensure_ascii=False,
        )

    result = dict(result)

    if result.get("success"):
        source_tree_value = result.get("source_tree")

        if not isinstance(source_tree_value, str):
            return json.dumps(
                {
                    "success": False,
                    "status": "invalid_source_tree",
                    "message": "Debian preparation returned no source_tree",
                },
                ensure_ascii=False,
            )

        source_tree = _resolve_normal_path(source_tree_value)

        if (
            source_tree == workspace_real
            or not _path_is_within(workspace_real, source_tree)
            or not os.path.isdir(source_tree)
        ):
            return json.dumps(
                {
                    "success": False,
                    "status": "unsafe_source_tree",
                    "message": (
                        "Prepared Debian source tree is outside the "
                        "registered workspace"
                    ),
                },
                ensure_ascii=False,
            )

        server_state.setdefault("workspace_edit_roots", {})[
            workspace_real
        ] = source_tree

        result["workspace_root"] = workspace_real
        result["active_edit_root"] = source_tree

    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def prepare_rpm_source_tool(package_path: str) -> str:
    """Materialize and register the sole safe RPM editable source root."""
    workspace_real = _resolve_normal_path(package_path)

    if workspace_real not in server_state.get(
        "workspace_sources", {}
    ):
        return json.dumps(
            {
                "success": False,
                "status": "unregistered_workspace",
                "message": (
                    "prepare_rpm_source_tool requires the "
                    "registered package workspace"
                ),
            },
            ensure_ascii=False,
        )

    result = prepare_rpm_source_workspace(workspace_real)

    if not isinstance(result, dict):
        return json.dumps(
            {
                "success": False,
                "status": "invalid_prepare_result",
                "message": "RPM source preparation returned no object",
            },
            ensure_ascii=False,
        )

    result = dict(result)

    if result.get("success"):
        source_tree_value = result.get("source_tree")

        if not isinstance(source_tree_value, str):
            return json.dumps(
                {
                    "success": False,
                    "status": "invalid_source_tree",
                    "message": "RPM preparation returned no source_tree",
                },
                ensure_ascii=False,
            )

        source_tree = _resolve_normal_path(source_tree_value)

        if (
            source_tree == workspace_real
            or not _path_is_within(workspace_real, source_tree)
            or not os.path.isdir(source_tree)
        ):
            return json.dumps(
                {
                    "success": False,
                    "status": "unsafe_source_tree",
                    "message": (
                        "Prepared RPM source tree is outside the "
                        "registered workspace"
                    ),
                },
                ensure_ascii=False,
            )

        server_state.setdefault("workspace_edit_roots", {})[
            workspace_real
        ] = source_tree

        result["workspace_root"] = workspace_real
        result["active_edit_root"] = source_tree

    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def extract_archive_tool(package_path: str):
    """Extract archive files in various formats"""
    try:
        archive_path = None
        package_dir = None
        archive_file = None

        if os.path.isfile(package_path):
            fmt, _ = get_archive_format(package_path)
            if fmt:
                archive_path = package_path
                package_dir = os.path.dirname(archive_path)
                archive_file = os.path.basename(archive_path)
            else:
                return f"Error: Unsupported archive format for '{package_path}'"

        elif os.path.isdir(package_path):
            found = False
            for item in os.listdir(package_path):
                item_path = os.path.join(package_path, item)
                if os.path.isfile(item_path):
                    fmt, _ = get_archive_format(item_path)
                    if fmt:
                        archive_path = item_path
                        archive_file = item
                        package_dir = package_path
                        found = True
                        break

            if not found:
                return f"Error: No supported archive in '{package_path}'"

        else:
            return f"Error: Invalid path '{package_path}'"

        extract_dir = os.path.join(package_dir, "extracted")
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)
        os.makedirs(extract_dir, exist_ok=True)

        fmt, mode = get_archive_format(archive_path)
        if fmt in ["tar.gz", "tar.xz", "tar.bz2"]:
            with tarfile.open(archive_path, mode) as tar:
                tar.extractall(extract_dir)
        elif fmt == "zip":
            with zipfile.ZipFile(archive_path, "r") as zip_ref:
                zip_ref.extractall(extract_dir)

        return f"Successfully extracted {archive_file} to {extract_dir}"

    except Exception as e:
        return f"Extraction failed: {str(e)}"


@mcp.tool()
def compress_to_archive_tool(package_path: str):
    """Compress extracted directory back to original format"""
    try:
        if not package_path or not isinstance(package_path, str):
            return "Error: package_path must be valid string"

        if "extracted" in package_path:
            return f"Error: package_path should not contain 'extracted'"

        extracted_dir = os.path.join(package_path, "extracted")
        if not os.path.exists(extracted_dir):
            return f"Error: Extracted directory '{extracted_dir}' not found"

        original_archive = None
        original_fmt = None
        for item in os.listdir(package_path):
            item_path = os.path.join(package_path, item)
            if os.path.isfile(item_path):
                fmt, _ = get_archive_format(item_path)
                if fmt:
                    original_archive = item_path
                    original_fmt = fmt
                    break

        if not original_archive:
            return f"Error: No original archive in '{package_path}'"

        original_filename = os.path.basename(original_archive)
        output_archive = os.path.join(package_path, f"{original_filename}")

        if os.path.exists(output_archive):
            os.remove(output_archive)

        if original_fmt in ["tar.gz", "tar.xz", "tar.bz2"]:
            mode = SUPPORTED_FORMATS[original_fmt]["mode"].replace("r", "w")
            with tarfile.open(output_archive, mode) as tar:
                for item in os.listdir(extracted_dir):
                    item_path = os.path.join(extracted_dir, item)
                    tar.add(item_path, arcname=item)
        elif original_fmt == "zip":
            with zipfile.ZipFile(output_archive, "w", zipfile.ZIP_DEFLATED) as zip_ref:
                for root, _, files in os.walk(extracted_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, extracted_dir)
                        zip_ref.write(file_path, arcname)

        shutil.rmtree(extracted_dir)
        return f"Success: Compressed to {output_archive}"

    except Exception as e:
        return f"Compression failed: {str(e)}"


@mcp.tool()
def upload_file_to_obs_tool(package_path: str):
    """Upload repaired package to OBS"""
    if not os.path.isdir(package_path):
        return f"Error: '{package_path}' is not a directory"

    has_spec = any(
        f.endswith(".spec")
        for f in os.listdir(package_path)
        if os.path.isfile(os.path.join(package_path, f))
    )
    if not has_spec:
        return f"Error: No .spec file in '{package_path}'"

    package_name = os.path.basename(package_path)
    try:
        obs_result = main_upload(package_name, package_path)
        if "error" in str(obs_result).lower():
            return f"Upload failed: {obs_result}"
        return f"Upload successful. Result: {obs_result}"
    except Exception as e:
        return f"Upload error: {str(e)}"


@mcp.tool()
def check_build_result(input_dir: str, package_name: str):
    """Check build result in OBS"""
    try:
        obs_result = check_main(input_dir, package_name)
        return f"Build result: {obs_result}"
    except Exception as e:
        return f"Build check error: {str(e)}"



def _write_feedback_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp.{os.getpid()}"

    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(content)

    os.replace(temporary, path)


def _write_feedback_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp.{os.getpid()}"

    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")

    os.replace(temporary, path)


def _publish_build_feedback(workspace: str, build_result) -> dict:
    """
    Publish trusted, bounded build feedback inside the Agent workspace.

    The backend's canonical result remains untouched. A skipped unchanged
    call does not overwrite the latest effective build feedback.
    """
    payload = dict(build_result.to_dict())

    feedback_dir = os.path.join(
        workspace,
        ".buildbench-feedback",
        "latest",
    )
    os.makedirs(feedback_dir, exist_ok=True)

    last_call_path = os.path.join(
        feedback_dir,
        "last-call.json",
    )
    _write_feedback_json(last_call_path, payload)

    status = str(
        payload.get("status") or "unknown"
    ).strip().lower()

    skipped = status == "skipped_unchanged_input"
    published = {
        "feedback_dir": feedback_dir,
        "feedback_last_call_path": last_call_path,
    }

    if not skipped:
        result_copy = os.path.join(
            feedback_dir,
            "build-result.json",
        )
        _write_feedback_json(result_copy, payload)
        published["feedback_result_path"] = result_copy

        log_path = payload.get("log_path")

        if (
            isinstance(log_path, str)
            and os.path.isfile(log_path)
        ):
            with open(
                log_path,
                "r",
                encoding="utf-8",
                errors="replace",
            ) as handle:
                log_content = handle.read()

            # Keep a bounded tail for model context efficiency.
            bounded_tail = log_content[-60000:]

            feedback_log = os.path.join(
                feedback_dir,
                "build-log-tail.txt",
            )
            _write_feedback_text(
                feedback_log,
                bounded_tail,
            )
            published["feedback_log_path"] = feedback_log

        candidate_dirs = []

        result_path = payload.get("result_path")

        if (
            isinstance(result_path, str)
            and os.path.isdir(result_path)
        ):
            candidate_dirs.append(result_path)

        if isinstance(log_path, str):
            candidate_dirs.append(
                os.path.dirname(log_path)
            )

        for directory in candidate_dirs:
            diagnostics_source = os.path.join(
                directory,
                "build-diagnostics.json",
            )

            if not os.path.isfile(diagnostics_source):
                continue

            diagnostics_destination = os.path.join(
                feedback_dir,
                "build-diagnostics.json",
            )

            with open(
                diagnostics_source,
                "r",
                encoding="utf-8",
                errors="replace",
            ) as handle:
                diagnostics_content = handle.read()

            _write_feedback_text(
                diagnostics_destination,
                diagnostics_content,
            )

            published[
                "feedback_diagnostics_path"
            ] = diagnostics_destination
            break

        summary = dict(payload)
        summary.update(published)

        summary_path = os.path.join(
            feedback_dir,
            "summary.json",
        )
        _write_feedback_json(summary_path, summary)
        published["feedback_summary_path"] = summary_path

    else:
        for key, name in (
            ("feedback_summary_path", "summary.json"),
            ("feedback_result_path", "build-result.json"),
            ("feedback_log_path", "build-log-tail.txt"),
            (
                "feedback_diagnostics_path",
                "build-diagnostics.json",
            ),
        ):
            candidate = os.path.join(
                feedback_dir,
                name,
            )

            if os.path.isfile(candidate):
                published[key] = candidate

    payload.update(published)
    return payload


@mcp.tool()
def run_build_validation_tool(package_path: str) -> str:
    """Build a registered package and publish Agent-readable feedback."""
    workspace, _, candidate = _registered_workspace_for_path(
        package_path
    )

    if workspace is None or candidate != workspace:
        return json.dumps(
            {
                "success": False,
                "status": "invalid_workspace",
                "message": (
                    "run_build_validation_tool requires the exact "
                    "registered Agent workspace"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    result = run_build_validation(candidate)
    payload = _publish_build_feedback(
        workspace,
        result,
    )

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    )


@mcp.tool()
def get_packages_to_process(base_dir: str, result_dir: str) -> str:
    """
    obtain the packages to process
    Args:
        base_dir: the base directory
        result_dir: the result storge directory
    Returns:
        a JSON string, containing the package list
    """
    try:
        if not os.path.exists(base_dir):
            return json.dumps(
                {
                    "success": False,
                    "message": f"Base directory not found: {base_dir}",
                    "packages": [],
                }
            )

        packages = [
            item
            for item in os.listdir(base_dir)
            if os.path.isdir(os.path.join(base_dir, item))
        ]

        return json.dumps(
            {
                "success": True,
                "message": f"Found {len(packages)} packages",
                "packages": packages,
            }
        )
    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "message": f"Error getting packages: {str(e)}",
                "packages": [],
            }
        )


if __name__ == "__main__":
    mcp.run(transport="stdio")
