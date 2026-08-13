"""baseline_patch.py — 纯 LLM 推理基线（Patch 模式，无 MCP，无 function calling tools）

LLM 不调用任何工具，只做纯推理输出 unified diff 格式的修复方案。
框架负责所有基础设施：读文件、应用 diff patch、构建检查（Docker 或 OBS）。
"""

import os
import re
import json
import shutil
import shlex
import time
import traceback
import tarfile
import zipfile
import hashlib
import subprocess
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import yaml
from dotenv import load_dotenv
import requests
from openai import OpenAI

load_dotenv(".env")
with open("config/info.yaml", "r") as f:
    info = yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Docker build helper constants (aligned with Build-bench docker.py)
# ---------------------------------------------------------------------------

_WORKSPACE_METADATA = ".buildbench-case.json"
_WORKSPACE_EXCLUDES = {
    _WORKSPACE_METADATA,
    "log_failed.txt",
    "manifest.json",
    "extracted",
    "result",
    "results",
    "artifacts",
}

# 失败构建日志分段注入策略（防止超大日志挤爆 num_ctx）
_LOG_MAX_CHARS = 96000        # 注入总上限（字符）
_LOG_HEAD_CHARS = 6000        # 保留头部（构建环境/依赖解析阶段）
_LOG_TAIL_CHARS = 72000       # 保留尾部（错误密集区，随总上限增加）
_LOG_ERROR_RE = re.compile(
    r"error:|Error|fatal|FAILED|failed|undefined reference|No such file|"
    r"cannot find|cannot open|dpkg: error|make: \*\*\*|ninja: error|"
    r"collect2:|ld returned|undeclared|not found|unmet|unresolvable|"
    r"fatal error|Werror"
)

# 模式 → prompt 文件映射（parallel_runner 与各基线共用）
# 各模式使用不同的提示词文件，同前缀代表同系列（如 spec 系列共享 spec 规则策略）
MODE_PROMPT_MAP = {
    "full": "prompts/full_file_generation_json.txt",
    "patch": "prompts/patch_generation_json.txt",
    "spec": "prompts/spec_generation_json.txt",
    "spec_patch": "prompts/spec_patch_generation_json.txt",
}


def _truncate_build_log(text: str) -> str:
    """对失败构建日志应用分段截断。

    大日志（> _LOG_MAX_CHARS）保留「头部（环境/依赖解析）+ 中间错误特征行 + 尾部（错误密集区）」，
    中间整段省略并标注，防止 100KB-1MB 的失败日志挤爆上下文窗口。
    日志较短时原样返回，零副作用。
    """
    if len(text) <= _LOG_MAX_CHARS:
        return text

    head = text[:_LOG_HEAD_CHARS]
    tail = text[-_LOG_TAIL_CHARS:]
    # 中间省略区：只挑错误特征行（去重保序），占用剩余预算
    mid_budget = _LOG_MAX_CHARS - _LOG_HEAD_CHARS - _LOG_TAIL_CHARS
    middle_lines: List[str] = []
    seen = set()
    for line in text[_LOG_HEAD_CHARS:-_LOG_TAIL_CHARS].splitlines():
        if _LOG_ERROR_RE.search(line):
            line = line.strip()
            if line and line not in seen:
                seen.add(line)
                middle_lines.append(line)
                mid_budget -= len(line) + 1
                if mid_budget <= 0:
                    break
    middle = "\n".join(middle_lines) if middle_lines else \
        "(被省略的中间区间内未找到错误特征行)"
    return (
        f"{head}\n"
        f"\n... [日志已分段截断：原文 {len(text)} 字符；保留头部 {_LOG_HEAD_CHARS} 字符 + "
        f"中间错误特征行 + 尾部 {_LOG_TAIL_CHARS} 字符] ...\n"
        f"{middle}\n"
        f"... [中间省略区结束，以下为日志尾部] ...\n"
        f"{tail}"
    )


def _copy_or_link(source: str, destination: str) -> str:
    try:
        os.link(source, destination)
        return destination
    except OSError:
        return shutil.copy2(source, destination)


def _copy_case(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        copy_function=_copy_or_link,
    )


def _copy_workspace_input(workspace: Path, input_dir: Path) -> None:
    if input_dir.exists():
        shutil.rmtree(input_dir)
    input_dir.mkdir(parents=True)

    for source in workspace.iterdir():
        if source.name in _WORKSPACE_EXCLUDES or source.name.startswith(".build-case-"):
            continue
        destination = input_dir / source.name
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        elif source.is_file():
            shutil.copy2(source, destination)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _refresh_staged_source_map(staged_case: Path) -> None:
    map_path = staged_case / "evidence" / "source-filename-map.json"
    if not map_path.is_file():
        return

    entries = json.loads(map_path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        return

    input_dir = staged_case / "input"
    refreshed: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            refreshed.append(entry)
            continue
        packaged_name = entry.get("packaged_name")
        if not isinstance(packaged_name, str):
            refreshed.append(entry)
            continue
        source = input_dir / packaged_name
        if not source.is_file() or source.is_symlink():
            continue
        updated = dict(entry)
        updated["sha256"] = _sha256(source)
        refreshed.append(updated)

    temporary_map = map_path.with_name(
        f".{map_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary_map.write_text(
            json.dumps(refreshed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_map, map_path)
    finally:
        temporary_map.unlink(missing_ok=True)


def _load_workspace_metadata(workspace: Path) -> dict[str, Any]:
    metadata_path = workspace / _WORKSPACE_METADATA
    if not metadata_path.is_file():
        return {}
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Invalid workspace metadata: {metadata_path}") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid workspace metadata: {metadata_path}")
    return data


# ---------------------------------------------------------------------------
# LLMConfig — 与原版一致
# ---------------------------------------------------------------------------

class LLMConfig:
    def __init__(
        self,
        provider: str,
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self.provider = provider.lower()
        self.model = model

        if api_key and base_url:
            self.api_key = api_key
            self.base_url = base_url
        else:
            self.api_key = os.getenv("API_KEY")
            self.base_url = os.getenv("API_BASE_URL")

        if not self.api_key:
            raise RuntimeError(
                f"Missing API key for provider={self.provider}. Check your environment.")

    def make_client(self) -> OpenAI:
        if self.base_url:
            return OpenAI(api_key=self.api_key, base_url=self.base_url)
        return OpenAI(api_key=self.api_key)


# ---------------------------------------------------------------------------
# 压缩/解压工具
# ---------------------------------------------------------------------------

SUPPORTED_FORMATS = {
    "tar.gz": {"extensions": [".tar.gz", ".tgz"], "mode": "r:gz"},
    "tar.xz": {"extensions": [".tar.xz", ".txz"], "mode": "r:xz"},
    "tar.bz2": {"extensions": [".tar.bz2", ".tbz"], "mode": "r:bz2"},
    "zip": {"extensions": [".zip"], "mode": "r"},
}

TEXT_EXTENSIONS = {
    ".spec", ".cabal", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx",
    ".py", ".rs", ".go", ".java", ".ts", ".js", ".tsx", ".jsx",
    ".txt", ".md", ".rst", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg",
    ".cmake", ".make", ".mak", ".mk", ".sh", ".bash", ".zsh",
    ".hs", ".lhs", ".html", ".xml", ".css", ".scss", ".sass",
    ".patch", ".diff", ".changes", "_constraints", "manifest.json",
}


def _get_archive_format(file_path: str):
    for fmt, info in SUPPORTED_FORMATS.items():
        for ext in info["extensions"]:
            if file_path.lower().endswith(ext):
                return fmt, info["mode"]
    return None, None


def _is_text_file(filepath: str) -> bool:
    base = os.path.basename(filepath).lower()
    for ext in TEXT_EXTENSIONS:
        if base.endswith(ext):
            return True
    if "." not in base and os.path.isfile(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as test_f:
                test_f.read(1024)
            return True
        except (UnicodeDecodeError, Exception):
            return False
    return False


def _extract_archive(package_path: str) -> Optional[str]:
    if os.path.isfile(package_path):
        archives = [package_path]
    elif os.path.isdir(package_path):
        archives = []
        for item in sorted(os.listdir(package_path)):
            item_path = os.path.join(package_path, item)
            if os.path.isfile(item_path):
                fmt, _ = _get_archive_format(item_path)
                if fmt:
                    archives.append(item_path)
    else:
        return None
    if not archives:
        return None

    extract_dir = os.path.join(
        os.path.dirname(archives[0]) if os.path.isfile(package_path) else package_path,
        "extracted",
    )
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir)
    os.makedirs(extract_dir, exist_ok=True)

    for archive_path in archives:
        fmt, mode = _get_archive_format(archive_path)
        if fmt in ["tar.gz", "tar.xz", "tar.bz2"]:
            with tarfile.open(archive_path, mode) as tar:
                tar.extractall(extract_dir)
        elif fmt == "zip":
            with zipfile.ZipFile(archive_path, "r") as zip_ref:
                zip_ref.extractall(extract_dir)
    return extract_dir


def _compress_archive(package_path: str) -> str:
    extracted_dir = os.path.join(package_path, "extracted")
    if not os.path.exists(extracted_dir):
        return f"Error: Extracted directory '{extracted_dir}' not found"

    original_archive = None
    original_fmt = None
    for item in os.listdir(package_path):
        item_path = os.path.join(package_path, item)
        if os.path.isfile(item_path):
            fmt, _ = _get_archive_format(item_path)
            if fmt:
                original_archive = item_path
                original_fmt = fmt
                break

    if not original_archive:
        return f"Error: No original archive in '{package_path}'"

    original_filename = os.path.basename(original_archive)
    output_archive = os.path.join(package_path, original_filename)

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
    return f"Compressed to {output_archive}"


# ---------------------------------------------------------------------------
# 重压缩（方案 A）：Docker 构建前把 workspace 副本 extracted/ 的修改写回归档
# 对齐原版 client.py 的 compress_to_archive_tool 契约：
#   LLM 修改 extracted/ 源码 → 重压缩回归档 → 构建从归档解压 → 修改生效。
# 只修改 workspace 副本（temp_work_dir/{pkg}），原始 case（base_dir/{pkg}/input）只读。
# ---------------------------------------------------------------------------

def _tar_top_level_names(archive_path: str) -> set:
    """返回 tar 归档的顶层条目名（用于把 extracted/ 子目录匹配回源归档）。"""
    fmt, mode = _get_archive_format(archive_path)
    if fmt not in ("tar.gz", "tar.xz", "tar.bz2"):
        return set()
    try:
        with tarfile.open(archive_path, mode) as tar:
            return {m.name.split("/", 1)[0] for m in tar.getmembers()}
    except (tarfile.TarError, OSError):
        return set()


def _checksum_of(path: str, algo: str) -> str:
    """计算文件的 md5 / sha1 / sha256 校验和（.dsc 三个字段各用其一）。"""
    digest = hashlib.new(algo)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repack_tar_entry(archive_path: str, extracted_dir: str, entry: str) -> str:
    """把 extracted_dir/<entry> 重打包覆盖 archive_path，并删除 extracted 中的源目录。"""
    fmt, mode = _get_archive_format(archive_path)
    if fmt not in ("tar.gz", "tar.xz", "tar.bz2"):
        return f"unsupported archive format: {archive_path}"
    tmp_path = f"{archive_path}.recompress.{uuid.uuid4().hex[:8]}"
    write_mode = mode.replace("r", "w")
    with tarfile.open(tmp_path, write_mode) as tar:
        tar.add(os.path.join(extracted_dir, entry), arcname=entry)
    os.replace(tmp_path, archive_path)
    shutil.rmtree(os.path.join(extracted_dir, entry))
    return f"{os.path.basename(archive_path)}<={entry}/"


def _sync_dsc_checksums(dsc_path: str, archive_names: set) -> str:
    """同步 .dsc 中 Files / Checksums-Sha1 / Checksums-Sha256 条目的 hash 与 size。"""
    algo_by_section = {
        "files": "md5",
        "checksums-sha1": "sha1",
        "checksums-sha256": "sha256",
    }
    header_re = re.compile(
        r"^(files|checksums-sha1|checksums-sha256)\s*:", re.IGNORECASE
    )
    # 校验和段条目：<hash> <size> <filename>（容忍有/无前导空格，两种格式都能识别）
    entry_re = re.compile(r"^\s*([0-9a-fA-F]+)\s+(\d+)\s+(\S+)\s*$")
    try:
        with open(dsc_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return f".dsc unreadable: {dsc_path}"

    dsc_dir = os.path.dirname(dsc_path)
    section = None
    changed = False
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if section:
            m = entry_re.match(stripped)
            if m:
                fname = m.group(3)
                if fname in archive_names:
                    archive_path = os.path.join(dsc_dir, fname)
                    if os.path.isfile(archive_path):
                        # dpkg 格式要求 Files/Checksums-* 段条目以单个前导空格开头
                        # （stripped.split() 会丢弃原行前导空白，重写时必须补回）
                        # 注意：此处前导空格是 dpkg-source 硬性格式要求，删除会导致
                        # "line with unknown format (not field-colon-value)" 构建失败
                        new_entry = (
                            f" {_checksum_of(archive_path, algo_by_section[section])} "
                            f"{os.path.getsize(archive_path)} {fname}"
                        )
                        if new_entry != stripped:
                            lines[i] = new_entry + "\n"
                            changed = True
                continue
            section = None
        match = header_re.match(stripped)
        if match:
            section = match.group(1).lower()

    if changed:
        tmp_path = f"{dsc_path}.recompress.{uuid.uuid4().hex[:8]}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp_path, dsc_path)
        return f"synced .dsc checksums: {sorted(archive_names)}"
    return "dsc checksums unchanged"


def _recompress_workspace(package_path: str) -> str:
    """Docker 构建前把 workspace 副本中 extracted/ 的修改重压缩回归档。

    - rpm（单归档）：整体重打包 extracted/ → 覆盖归档并删除 extracted/（复用 _compress_archive）。
    - deb（多归档）：extracted/debian → 重打包回 *.debian.tar.*；
      其余源码子目录按顶层条目匹配 → 重打包回对应 *.orig.tar.*；
      随后同步 .dsc 的 Files / Checksums-Sha1 / Checksums-Sha256。
    无 extracted/ 时返回空串（无操作，不改变现有行为）。
    """
    extracted_dir = os.path.join(package_path, "extracted")
    if not os.path.exists(extracted_dir):
        return ""

    dsc_files = [
        f for f in os.listdir(package_path)
        if f.endswith(".dsc") and os.path.isfile(os.path.join(package_path, f))
    ]
    if not dsc_files:
        # rpm / 单归档：extracted/ 整体来自唯一归档
        return f"rpm: {_compress_archive(package_path)}"

    dsc_path = os.path.join(package_path, dsc_files[0])
    repacked: set = set()
    messages = []

    # deb: extracted/debian → *.debian.tar.*
    if os.path.isdir(os.path.join(extracted_dir, "debian")):
        for f in sorted(os.listdir(package_path)):
            if re.search(r"\.debian\.tar\.(?:gz|xz|bz2)$", f, re.IGNORECASE):
                archive_path = os.path.join(package_path, f)
                if os.path.isfile(archive_path):
                    messages.append(
                        _repack_tar_entry(archive_path, extracted_dir, "debian")
                    )
                    repacked.add(f)
                    break

    # deb: extracted/ 下其余源码子目录 → 顶层条目匹配的 *.orig.tar.*
    for entry in sorted(os.listdir(extracted_dir)):
        entry_path = os.path.join(extracted_dir, entry)
        if not os.path.isdir(entry_path) or entry == "debian":
            continue
        for f in sorted(os.listdir(package_path)):
            if not re.search(r"\.orig(?:-\d+)?\.tar\.(?:gz|xz|bz2)$", f, re.IGNORECASE):
                continue
            archive_path = os.path.join(package_path, f)
            if os.path.isfile(archive_path) and entry in _tar_top_level_names(archive_path):
                messages.append(_repack_tar_entry(archive_path, extracted_dir, entry))
                repacked.add(f)
                break

    if repacked:
        messages.append(_sync_dsc_checksums(dsc_path, repacked))
    if os.path.isdir(extracted_dir) and not os.listdir(extracted_dir):
        shutil.rmtree(extracted_dir)
        messages.append("removed empty extracted/")
    if not messages:
        messages.append("no matching archives to recompress")
    return f"deb: {'; '.join(messages)}"


def _unified_diff_to_line_ops(diff_text: str) -> list:
    """将 unified diff 文本转换为行级操作列表（对齐原版 MCP 的 modification_history 格式）"""
    ops = []
    for line in diff_text.splitlines():
        if line.startswith("---") or line.startswith("+++") or line.startswith("@@") or line.startswith("diff "):
            continue
        if line.startswith("-"):
            ops.append({"operation": "delete", "line_number": len(ops) + 1, "content": line[1:][:200]})
        elif line.startswith("+"):
            ops.append({"operation": "add", "line_number": len(ops) + 1, "content": line[1:][:200]})
    return ops


# ---------------------------------------------------------------------------
# Unified Diff 解析与应用
# ---------------------------------------------------------------------------

def _strip_thinking(text: str) -> str:
    """剥离 thinking 模型可能输出的 <think> 标签内容"""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


def _is_local_ollama(base_url: str) -> bool:
    """判断 base_url 是否指向本地 Ollama（决定走原生 /api/chat 还是 OpenAI 兼容云 API）"""
    host = base_url.split("://")[-1].split("/")[0].split(":")[0].lower()
    return host in ("127.0.0.1", "localhost", "0.0.0.0", "::1")


# ---------------------------------------------------------------------------
# Token 消耗记录（输出到 temp-guidance/token消耗/，JSONL 追加）
# ---------------------------------------------------------------------------

_TOKEN_USAGE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "temp-guidance", "token消耗",
)
_TOKEN_USAGE_PATH = os.path.join(_TOKEN_USAGE_DIR, "token_usage.jsonl")

# 内存中的 token 消耗记录，按 (package, attempt, mode) 索引，用于写入 summary.json
_TOKEN_USAGE_BY_ATTEMPT: Dict[str, Dict[str, Any]] = {}


def _record_token_usage(meta: Optional[Dict[str, Any]], usage: Dict[str, Any]) -> None:
    """追加一条 token 消耗记录到 temp-guidance/token消耗/token_usage.jsonl，
    同时保存到内存中的 _TOKEN_USAGE_BY_ATTEMPT 供 summary.json 使用。

    每行一条 JSON：timestamp + meta（package/attempt/mode/model 等）+ usage：
    - prompt_tokens：输入 token 数
    - completion_tokens：输出 token 数
    - total_tokens：输入 + 输出合计
    - cached_tokens：缓存命中的输入部分（云 API 有 prompt caching 时非 0）
    """
    if not usage:
        return
    os.makedirs(os.path.dirname(_TOKEN_USAGE_PATH), exist_ok=True)
    record = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
    if meta:
        record.update(meta)
    record.update(usage)
    with open(_TOKEN_USAGE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # 更新内存字典，供 summary.json 使用
    if meta:
        key = f"{meta.get('package')}_{meta.get('attempt')}_{meta.get('mode')}"
        _TOKEN_USAGE_BY_ATTEMPT[key] = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "cached_tokens": usage.get("cached_tokens"),
        }


def _cloud_chat_openai_compat(
    model: str,
    messages: List[Dict[str, str]],
    max_tokens: int,
    response_format: Optional[Dict],
    timeout_seconds: int,
    retries: int,
    api_base: str,
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    """调用 OpenAI 兼容云 API（/v1/chat/completions），如 TokRouter。

    云 API 无本地 Ollama 的 GIN 超时 / 调度竞态问题，
    但保留 retries 重试以应对瞬时 5xx / 网络抖动。
    meta: 附加到 token 消耗记录的信息（如 package/attempt/mode），可为空。
    """
    import time as _time

    api_key = os.getenv("API_KEY")
    client = OpenAI(
        api_key=api_key,
        base_url=api_base,
        timeout=timeout_seconds,
        max_retries=0,
    )
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    # 输出格式约束（纯 LLM 基线，用户已确认方案：纯 prompt + JSON 模板）：
    # - 云 API（TokRouter 等）不传 response_format：json_schema 与 json_object
    #   均实测触发 502（Upstream service error），属确定性功能不可用，重试无意义。
    # - tools + tool_choice 虽能强约束输出，但违反"纯 LLM 基线不传 tools"的
    #   合规要求（见 BuildBench-四条基线搭建技术文档.md：无工具由框架层保证）。
    # - 因此格式统一由 system prompt 的 JSON 模板约束，结构由
    #   _parse_llm_output_json 容错解析兜底（实测 gpt-5.6-luna 遵守良好）。
    # 注：response_format 参数保留仅为兼容调用方签名，实际不再使用。

    last_exc = None
    for attempt_retry in range(1, retries + 1):
        try:
            resp = client.chat.completions.create(**kwargs)
            usage = resp.usage
            if usage is not None:
                cached = 0
                details = getattr(usage, "prompt_tokens_details", None)
                if details is not None:
                    cached = getattr(details, "cached_tokens", 0) or 0
                _record_token_usage(meta, {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                    "cached_tokens": cached,
                })
            return resp.choices[0].message.content or ""
        except Exception as e:
            last_exc = e
            if attempt_retry < retries:
                _time.sleep(30)
                continue
            raise
    raise RuntimeError(f"Cloud API call failed after {retries} retries") from last_exc


def _ollama_chat(
    model: str,
    messages: List[Dict[str, str]],
    max_tokens: int = 16384,
    response_format: Optional[Dict] = None,
    num_ctx: int = 32768,
    timeout_seconds: int = 7200,
    retries: int = 2,
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    """LLM 调用统一入口：本地 Ollama 原生 /api/chat，或 OpenAI 兼容云 API。

    - API_BASE_URL 指向本地（127.0.0.1/localhost）→ 走 Ollama 原生 /api/chat，
      绕过 OpenAI 兼容层的 GIN 超时限制。
    - 否则视为 OpenAI 兼容云 API（如 TokRouter）→ 走 /v1/chat/completions。

    返回 LLM 输出的原始文本（已剥离 thinking 标签）。
    timeout_seconds=7200: 单次 LLM 调用超时（防止进程无限卡死）。
    retries=2: 遇到 5xx / 网络错误时自动重试 1 次（30s 后）。

    输出格式约束（纯 LLM 基线，方案：纯 prompt + JSON 模板）：
    本地与云 API 均不传 format / response_format（云 API 上 json_schema 与
    json_object 均实测 502），格式由 system prompt 的 JSON 模板约束，
    解析由 _extract_json_object / _parse_llm_output_json 容错兜底。
    response_format 参数保留仅为兼容调用方签名，实际不再使用。

    meta: 附加到 token 消耗记录的信息（如 package/attempt/mode），可为空。
    """
    import time as _time

    api_base = os.getenv("API_BASE_URL", "http://127.0.0.1:11434/v1")
    if not _is_local_ollama(api_base):
        return _cloud_chat_openai_compat(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            response_format=response_format,
            timeout_seconds=timeout_seconds,
            retries=retries,
            api_base=api_base,
            meta=meta,
        )
    ollama_base = api_base.replace("/v1", "").rstrip("/")
    url = f"{ollama_base}/api/chat"

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "num_ctx": num_ctx,
            "num_predict": max_tokens,
        },
    }
    # 不设置 GBNF format：本地与云 API 统一走 prompt 模板 + 容错解析，
    # 保证两条路径行为一致（详见本函数 docstring）。

    last_exc = None
    for attempt_retry in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=timeout_seconds)
            resp.raise_for_status()
            data = resp.json()
            # 记录本地 Ollama token 消耗（prompt_eval_count=输入 / eval_count=输出）
            if meta and "prompt_eval_count" in data:
                prompt_tokens = data.get("prompt_eval_count", 0)
                completion_tokens = data.get("eval_count", 0)
                _record_token_usage(meta, {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "cached_tokens": None,
                })
            return data["message"]["content"]
        except requests.exceptions.HTTPError as e:
            last_exc = e
            if e.response is not None and e.response.status_code == 500 and attempt_retry < retries:
                delay = 30  # 固定 30s，一次重试足够
                _time.sleep(delay)
                continue
            raise
        except Exception as e:
            last_exc = e
            raise

    raise RuntimeError(f"Ollama call failed after {retries} retries") from last_exc


def _repair_stray_closing_braces(text: str) -> str:
    """修复 LLM 输出中对象闭合多出的右花括号（实测 gpt-5.6-luna 偶发）。

    模型有时在 JSON 对象闭合时多输出一个 }（如 "}}" 或 "} }"），导致
    json.loads 失败。此函数仅处理「字符串结束引号之后紧邻的杂散 }」，
    不触及字符串内容（JSON 内引号已被转义为 \\"，不会被匹配）。
    """
    return re.sub(r'"\}\s*\}', '"}' , text)


def _extract_json_object(text: str) -> Any:
    """容错解析 LLM 输出中的 JSON 对象。

    依次尝试：直接 json.loads → 剥离 ```json``` 代码块 → 截取首个
    { 到最后一个 } 的子串；每个候选解析失败后再尝试「修复杂散 }」版本。
    全部失败返回 None。
    用于纯 prompt 输出模式（无 response_format 强约束）的容错兜底。
    """
    if not isinstance(text, str):
        return None
    candidates = [text.strip()]
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    # 候选：diff 字符串结束引号（文本中最后一个 "）之后接了杂散闭合符
    # （实测 gpt-5.6-luna 偶发 `"}]}]}`），丢弃杂散闭合符并按标准结构补 `}]}`。
    last_q = text.rfind('"')
    if last_q != -1:
        tail = text[last_q + 1:]
        if tail and all(ch in "]} \t\n" for ch in tail):
            candidates.append(text[:last_q + 1] + "}]}")
    for cand in candidates:
        for candidate in (cand, _repair_stray_closing_braces(cand)):
            try:
                return json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
    return None


def _parse_llm_output_json(text: str) -> Dict[str, List[Dict]]:
    """从 LLM 输出中解析 JSON (json_schema / json_object + prompt 模板保证格式)。

    本地 Ollama 走 json_schema（GBNF 强约束，格式 100% 保证）；
    云 API 走 json_object 降级（合法 JSON 由格式约束保证，结构由
    prompt JSON 模板 + 本函数容错兜底）。
    返回与 _parse_unified_diff 相同格式的 Dict[str, List[Dict]]
    """
    data = _extract_json_object(text)
    if data is None or not isinstance(data, dict):
        return {}
    if "diffs" not in data or not isinstance(data["diffs"], list):
        return {}
    result = {}
    for entry in data["diffs"]:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", "")).strip()
        diff_str = str(entry.get("diff", ""))
        if path and diff_str:
            # 方式 1: unified diff
            parsed = _parse_unified_diff(diff_str)
            # 方式 2: ed 脚本格式兜底
            if not parsed:
                parsed = _parse_ed_to_hunks(diff_str, path)
            # 追加 hunks 而非覆盖（同一个文件可能有多个 diff entry）
            for k, v in parsed.items():
                result.setdefault(k, []).extend(v)
    return result

def _parse_unified_diff(diff_text: str) -> Dict[str, List[Dict]]:
    """解析 unified diff 文本，返回 {file_path: [hunks]}"""
    diff_file_re = re.compile(r'^diff --git a/(.+?) b/(.+?)$')
    hunk_header_re = re.compile(r'^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@(.*)$')

    files = {}
    current_file = None
    current_hunks = []

    state = "idle"  # idle -> header -> hunks -> idle

    for line in diff_text.splitlines():
        # Check for new file header
        dfm = diff_file_re.match(line)
        if dfm or line.startswith("--- a/") or line.startswith("+++ b/"):
            if state == "hunks" and current_file and current_hunks:
                files[current_file] = current_hunks
            if dfm:
                current_file = dfm.group(1)
                current_hunks = []
                state = "header"
            elif line.startswith("--- a/"):
                # 兜底：无 diff --git 时，从 --- a/PATH 提取路径
                path = line[6:].strip()
                if path:
                    current_file = path
                    current_hunks = []
                    state = "header"
            continue

        # Check for hunk header
        hm = hunk_header_re.match(line)
        if hm:
            old_start = int(hm.group(1))
            old_count = int(hm.group(2)) if hm.group(2) else 1
            new_start = int(hm.group(3))
            new_count = int(hm.group(4)) if hm.group(4) else 1
            context_title = hm.group(5).strip()
            if current_file is not None:
                current_hunks.append({
                    "old_start": old_start,
                    "old_count": old_count,
                    "new_start": new_start,
                    "new_count": new_count,
                    "context": context_title,
                    "lines": [],
                })
                state = "hunks"
            continue

        # Within a hunk, collect lines
        if state == "hunks" and current_hunks:
            current_hunks[-1]["lines"].append(line)

        # Ignore other lines (index lines, empty lines between files, etc.)

    if current_file and current_hunks:
        files[current_file] = current_hunks

    return files


def _parse_ed_to_hunks(ed_text: str, file_path: str) -> Dict[str, List[Dict]]:
    """解析 ed 脚本格式，转换为 unified diff hunk 格式

    Ed 格式示例：
      24c24        修改第 24 行
      38c38,39     修改第 38 行（旧 1 行 → 新 2 行）
      67a70,75     在第 67 行后追加
      52c52        修改第 52 行

    返回与 _parse_unified_diff 相同格式的 Dict[str, List[Dict]]
    """
    ed_cmd_re = re.compile(r'^(\d+)(?:,(\d+))?([acd])')
    hunks = []
    lines = ed_text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]
        m = ed_cmd_re.match(line)
        if not m:
            i += 1
            continue

        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        cmd = m.group(3)
        i += 1

        old_lines = []
        new_lines_list = []

        while i < len(lines):
            l = lines[i]
            if ed_cmd_re.match(l):
                break
            if l == '---':
                i += 1
                continue
            if l.startswith('<'):
                content = l[1:]
                if content and content[0] in (' ', '\t'):
                    content = content[1:]
                old_lines.append(content)
                i += 1
                continue
            if l.startswith('>'):
                content = l[1:]
                if content and content[0] in (' ', '\t'):
                    content = content[1:]
                new_lines_list.append(content)
                i += 1
                continue
            i += 1

        if cmd == 'c':
            old_cnt = len(old_lines) if old_lines else (end - start + 1)
            hunks.append({
                "old_start": start,
                "old_count": old_cnt,
                "new_start": start,
                "new_count": len(new_lines_list),
                "context": "",
                "lines": [f"-{l}" for l in old_lines] + [f"+{l}" for l in new_lines_list],
            })
        elif cmd == 'a':
            hunks.append({
                "old_start": start,
                "old_count": 0,
                "new_start": start + 1,
                "new_count": len(new_lines_list),
                "context": "",
                "lines": [f"+{l}" for l in new_lines_list],
            })
        elif cmd == 'd':
            old_cnt = len(old_lines) if old_lines else (end - start + 1)
            hunks.append({
                "old_start": start,
                "old_count": old_cnt,
                "new_start": start,
                "new_count": 0,
                "context": "",
                "lines": [f"-{l}" for l in old_lines],
            })

    return {file_path: hunks} if hunks else {}


def _resolve_package_path(package_path: str, rel_path: str) -> Optional[str]:
    """解析 LLM 输出的相对路径，支持多种写法。

    按优先级尝试：
    1. direct:    package_path / rel_path
    2. extracted: package_path / extracted / rel_path  (LLM 漏了 extracted/ 前缀)
    3. deep:      package_path / extracted / <任意子目录> / rel_path (顶层版本目录)
    """
    candidates = [os.path.join(package_path, rel_path)]
    # 如果路径不以 extracted/ 开头，补上 extracted/ 尝试
    if not rel_path.startswith("extracted/"):
        candidates.append(os.path.join(package_path, "extracted", rel_path))
    # 如果路径以 extracted/ 开头，也尝试去掉前缀
    else:
        candidates.append(os.path.join(package_path, rel_path[len("extracted/"):]))

    for p in candidates:
        if os.path.exists(p):
            return p

    # 兜底：在 extracted/ 的各子目录中搜索（处理版本子目录如 PyBDSF-1.9.2/）
    extracted_dir = os.path.join(package_path, "extracted")
    if os.path.isdir(extracted_dir):
        search_rel = rel_path[len("extracted/"):] if rel_path.startswith("extracted/") else rel_path
        for entry in sorted(os.listdir(extracted_dir)):
            entry_path = os.path.join(extracted_dir, entry, search_rel)
            if os.path.exists(entry_path):
                return entry_path

    return candidates[0]  # 返回第一个候选路径（即使不存在）


def _apply_patch(package_path: str, parsed_diff: Dict[str, List[Dict]]) -> List[str]:
    """将解析后的 diff 应用到对应的文件，返回修改的文件列表

    对齐原版 server_patch.py 的 _apply_hunks_strict：
    正向遍历 hunk + line_offset 累计，而非反向遍历。
    """
    modified = []
    for file_path, hunks in parsed_diff.items():
        full_path = _resolve_package_path(package_path, file_path)
        if not os.path.exists(full_path):
            print(f"Warning: File not found for patching: {full_path}")
            continue

        with open(full_path, "r", encoding="utf-8") as f:
            original_lines = f.readlines()

        new_lines = list(original_lines)
        line_offset = 0
        ok = True

        # 正向遍历 hunk，用 line_offset 累积行号偏移（对齐原版 _apply_hunks_strict）
        for hunk in hunks:
            a_l = hunk["old_start"]            # 1-based line number from diff header
            # 容错：LLM 输出的 hunk 行号常与实际文件差 1-2 行（其看到的文件版本行号
            # 与真实文件不一致），在声明行号附近 ±2 行内搜索第一个能完整匹配的起点。
            # 匹配要求 hunk 全部 context/删除行与文件逐行精确一致，误命中风险低。
            deltas = (0, -1, 1, -2, 2)
            applied = False
            for delta in deltas:
                start = (a_l - 1) + line_offset + delta
                if start < 0 or start > len(new_lines):
                    continue
                cur = start
                replacement = []
                ok = True

                for raw in hunk["lines"]:
                    if raw.startswith("+"):
                        # 新增行：加入 replacement，不消耗源文件行
                        replacement.append(raw[1:] + "\n")
                    elif raw.startswith(" "):
                        # context 行：必须精确匹配
                        if cur >= len(new_lines):
                            ok = False
                            break
                        if new_lines[cur].rstrip("\n") != raw[1:]:
                            ok = False
                            break
                        replacement.append(new_lines[cur])
                        cur += 1
                    elif raw.startswith("-"):
                        # 删除行：必须精确匹配，不加入 replacement
                        if cur >= len(new_lines):
                            ok = False
                            break
                        if new_lines[cur].rstrip("\n") != raw[1:]:
                            ok = False
                            break
                        cur += 1

                if not ok:
                    continue

                # 应用替换：用 replacement 替换 new_lines[start:cur]
                before_len = len(new_lines)
                new_lines[start:cur] = replacement
                after_len = len(new_lines)
                # 累计行号偏移 = 实际增减行数 - hunk 消耗的源文件行数
                line_offset += (after_len - before_len) - (cur - start)
                applied = True
                if delta:
                    print(f"Note: hunk @{file_path}:{a_l} matched at line {start + 1} (offset {delta:+d})")
                break

            if not applied:
                print(f"Warning: Hunk context mismatch at {file_path}:{a_l}, skipping file")
                ok = False
                break

        if ok:
            with open(full_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            modified.append(file_path)

    return modified


# ---------------------------------------------------------------------------
# 模块级文件收集工具（baseline.py 与 baseline_patch.py 共用）
# ---------------------------------------------------------------------------

def _file_priority(filepath: str) -> int:
    """返回文件优先级（数字越小优先级越高）

    优先级逻辑：
    1. 按文件类型和名称确定基础优先级
    2. 特定目录上下文（如 debian/）会提升优先级
    """
    basename = os.path.basename(filepath).lower()
    priority: int

    if basename.endswith('.spec') or basename.endswith('.dsc'):
        priority = 0
    elif (basename.endswith('.cabal') or basename in (
        'makefile', 'cmakelists.txt', 'cmakelists', 'kbuild', 'meson.build', 'meson_options.txt',
        'configure', 'configure.ac', 'configure.in', 'gnumakefile',
    )):
        priority = 1
    elif basename.endswith(('.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hxx')):
        priority = 2
    elif basename.endswith(('.py', '.rs', '.go', '.java', '.hs', '.lhs')):
        priority = 3
    elif basename.endswith(('.sh', '.bash', '.zsh')):
        priority = 4
    elif basename.endswith(('.cfg', '.conf', '.ini', '.txt', '.md', '.rst')):
        priority = 5
    else:
        priority = 6

    # --- 目录上下文提升 ---
    # debian/ 目录下的文件对构建至关重要，提升 3 级
    if "/debian/" in filepath:
        priority = max(0, priority - 3)
    # 已知关键无扩展名文件名（不依赖目录上下文也能识别）
    if basename in ('rules', 'control', 'changelog', 'compat', 'copyright'):
        priority = min(priority, 1)

    return priority


def _read_file_safe(path: str) -> Optional[str]:
    """安全读取文本文件，返回 None 如果读取出错"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _collect_package_files(package_path: str, max_chars: int = 256000,
                            log_func=None) -> Dict[str, str]:
    """扫描提取后的目录，按优先级收集所有文本文件内容

    返回 {relative_path: content}，路径相对于 package_path（含 extracted/ 前缀）。
    max_chars 控制累计内容大小上限。
    log_func 是可选的回调函数，用于输出日志信息。
    """
    extracted = os.path.join(package_path, "extracted")
    if not os.path.exists(extracted):
        return {}

    # 1. 扫描所有文本文件
    candidates = []  # [(priority, file_size, full_path, rel_path)]
    for root, dirs, files in os.walk(extracted):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
        for f in files:
            full_path = os.path.join(root, f)
            if _is_text_file(full_path):
                rel_to_extracted = os.path.relpath(full_path, extracted)
                rel_to_package = os.path.join("extracted", rel_to_extracted)
                priority = _file_priority(rel_to_package)
                try:
                    fsize = os.path.getsize(full_path)
                except OSError:
                    fsize = 0
                candidates.append((priority, fsize, full_path, rel_to_package))

    # 包根目录（非 extracted/）的文件也加上
    for item in os.listdir(package_path):
        item_path = os.path.join(package_path, item)
        if os.path.isfile(item_path) and _is_text_file(item_path) and item != "log_failed.txt":
            priority = _file_priority(item)
            try:
                fsize = os.path.getsize(item_path)
            except OSError:
                fsize = 0
            candidates.append((priority, fsize, item_path, item))

    # 2. 按优先级排序，同优先级按文件大小升序（小文件优先）
    candidates.sort(key=lambda x: (x[0], x[1]))

    # 3. 读取内容，累计到 max_chars
    result = {}
    total_chars = 0
    included = []
    for _, _, full_path, rel_path in candidates:
        try:
            content = _read_file_safe(full_path)
            if content is None:
                continue
            if total_chars + len(content) <= max_chars:
                result[rel_path] = content
                total_chars += len(content)
                included.append(rel_path)
            else:
                remaining = max_chars - total_chars
                if remaining > 100:
                    result[rel_path] = content[:remaining] + "\n... (truncated)"
                    included.append(f"{rel_path} (truncated)")
                break  # 超出预算，停止
        except Exception:
            pass

    if log_func:
        log_func(f"Collected {len(result)} source files ({total_chars} chars): {included}")
    return result


# ---------------------------------------------------------------------------
# AutoRepairBaselinePatch — 纯 LLM 基线（Patch 模式）
# ---------------------------------------------------------------------------

class AutoRepairBaselinePatch:
    def __init__(
        self,
        llm: Optional[LLMConfig] = None,
        base_dir: Optional[str] = None,
        max_build_attempts: int = 3,
        prebuild: Optional[bool] = None,
        mode: str = "patch",
    ) -> None:
        self.llm_cfg = llm
        self.base_dir = base_dir or info["paths"]["base_dir"]
        self.result_dir = info["paths"]["result_dir"]
        self.log_dir = info["paths"]["log_dir"]
        self.temp_work_dir = info["paths"]["temp_work_dir"]
        self.max_build_attempts = max_build_attempts

        os.makedirs(self.result_dir, exist_ok=True)
        os.makedirs(self.temp_work_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)

        # 实验数据收集目录（保存在 temp-guidance，不污染 data/）
        base_dir_path = os.path.dirname(os.path.abspath(__file__))
        guidance_dir = os.path.join(base_dir_path, "..", "temp-guidance")
        self.debug_dir = os.path.join(guidance_dir, "llm_debug")
        self.experiments_root = os.path.join(guidance_dir, "experiments")
        self.mode = mode  # 区分实验模式（patch / spec / spec_patch）
        os.makedirs(self.debug_dir, exist_ok=True)
        os.makedirs(self.experiments_root, exist_ok=True)

        self.modification_history: Dict[str, List[Dict]] = {}
        self.timing_info: Dict[str, Dict[int, float]] = {}  # {package_name: {attempt: seconds}}

        # --- Docker build backend config ---
        build_cfg = info.get("build") or {}
        self.build_backend = os.getenv("BUILD_BENCH_BACKEND",
                                       build_cfg.get("backend", "obs")).strip().lower()
        # 预构建开关：参数优先，其次 config/build.prebuild，默认开启
        self.prebuild = build_cfg.get("prebuild", True) if prebuild is None else prebuild
        docker_cfg = dict(build_cfg.get("docker") or {})
        self.docker_validator_command = shlex.split(str(
            docker_cfg.get("validator_command",
                           "/home/zhaochenyu/buildbench_competition/docker-validator/bin/build-case-docker")
        ))
        self.docker_result_root = Path(
            docker_cfg.get("result_root", "run_records/docker-builds")
        ).expanduser()
        self.docker_staging_root = Path(
            docker_cfg.get("staging_root", "temp_workspace/docker-cases")
        ).expanduser()
        self.case_store_dir = (
            Path(str(docker_cfg["case_store_dir"])).expanduser()
            if docker_cfg.get("case_store_dir")
            else None
        )
        self.case_map = {
            str(key): Path(str(value)).expanduser()
            for key, value in dict(docker_cfg.get("case_map") or {}).items()
        }
        self._log("global", f"Build backend: {self.build_backend}")

    def _log(self, tag: str, msg: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        path = os.path.join(self.log_dir, f"{tag}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
        print(msg)

    def _get_packages_to_process(self) -> List[str]:
        if not os.path.exists(self.base_dir):
            self._log("global", f"Base directory not found: {self.base_dir}")
            return []
        packages = [
            item for item in os.listdir(self.base_dir)
            if os.path.isdir(os.path.join(self.base_dir, item))
        ]
        self._log("global", f"Found {len(packages)} packages: {packages}")
        return packages

    def _init_package_environment(self, package_name: str) -> Optional[Dict]:
        package_temp_dir = os.path.join(self.temp_work_dir, package_name)
        if os.path.exists(package_temp_dir):
            shutil.rmtree(package_temp_dir)
        os.makedirs(package_temp_dir, exist_ok=True)

        original = os.path.join(self.base_dir, package_name)
        if not os.path.exists(original):
            self._log(package_name, f"Original path not found: {original}")
            return None

        # Case 格式：从 input/ 子目录提取源码
        input_dir = os.path.join(original, "input")
        if os.path.isdir(input_dir):
            for item in os.listdir(input_dir):
                src_path = os.path.join(input_dir, item)
                dst_path = os.path.join(package_temp_dir, item)
                if os.path.isdir(src_path):
                    shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
                else:
                    shutil.copy2(src_path, dst_path)
        else:
            # 兼容旧格式：直接从包目录复制
            for item in os.listdir(original):
                src_path = os.path.join(original, item)
                dst_path = os.path.join(package_temp_dir, item)
                if os.path.isdir(src_path):
                    shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
                else:
                    shutil.copy2(src_path, dst_path)

        # 如果 canonical case 的 logs/ 中有构建失败日志，作为初始 log_failed.txt 提供给 LLM
        target_log = os.path.join(original, "logs", "docker-target-build.log")
        if os.path.isfile(target_log):
            shutil.copy2(target_log, os.path.join(package_temp_dir, "log_failed.txt"))
            self._log(package_name, f"Initial build failure log copied from {target_log}")

        result_file = os.path.join(self.result_dir, f"{package_name}_result.txt")
        self._log(package_name, f"Initialized environment at {package_temp_dir}")
        return {"package_path": package_temp_dir, "result_file": result_file}

    def _build_source_section(self, files: Dict[str, str]) -> str:
        parts = []
        for fname, content in files.items():
            parts.append(f"\n### {fname}\n```\n{content}\n```")
        return "\n".join(parts)

    def _get_directory_tree(self, package_path: str) -> str:
        """返回目录结构树（不含文件内容），供 LLM 按需请求文件"""
        lines = ["Directory structure (relative to package root):"]
        for root, dirs, files in os.walk(package_path):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
            rel = os.path.relpath(root, package_path)
            if rel == '.':
                for f in sorted(files):
                    fpath = os.path.join(root, f)
                    if _is_text_file(fpath) and os.path.exists(fpath):
                        lines.append(f"  {f}  ({os.path.getsize(fpath)} bytes)")
            else:
                lines.append(f"  {rel}/")
                for f in sorted(files):
                    fpath = os.path.join(root, f)
                    if _is_text_file(fpath) and os.path.exists(fpath):
                        lines.append(f"  {rel}/{f}  ({os.path.getsize(fpath)} bytes)")
        return '\n'.join(lines)

    def _build_history_section(self, package_name: str, attempt: int) -> str:
        """构建修改历史摘要，完全对齐原版 MCP 的 update_prompt_with_history_tool 格式

        只在 attempt > 1 时返回历史记录。
        """
        if attempt <= 1:
            return ""
        history = self.modification_history.get(package_name, [])
        if not history:
            return ""
        parts = ["\n\nPrevious modifications:"]
        for entry in history:
            fpath = entry.get("file_path", "")
            if not fpath:
                continue
            parts.append(f"File: {fpath}")
            parts.append("Changes:")
            diffs = entry.get("diff", [])
            if not diffs:
                parts.append(f"  (no diff recorded, build result: {entry.get('build_result', 'unknown')})")
            else:
                for change in diffs:
                    op = change.get("operation", "unknown")
                    line = change.get("line_number", "?")
                    content = change.get("content", "")[:200]
                    parts.append(f"- Line {line} ({op}): {content}")
            parts.append("")
        parts.append(
            f"\nAfter {len(history)} modification(s), build still failed. "
            "Analyze previous modifications and failures, then provide new repair plan."
        )
        return "\n".join(parts)

    def _get_build_log(self, package_path: str) -> str:
        """读取构建失败日志（应用分段截断策略），如果不存在则返回空字符串"""
        log_path = os.path.join(package_path, "log_failed.txt")
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    return _truncate_build_log(f.read())
            except Exception:
                pass
        return ""

    def _parse_and_apply_patch(self, package_path: str, llm_text: str) -> List[str]:
        """解析 LLM 输出的 JSON diff 并应用到文件

        json_schema 保证格式为 {"diffs": [{"path": "...", "diff": "..."}]}，
        只接受这一种格式。
        """
        parsed_json = _parse_llm_output_json(llm_text)
        if not parsed_json:
            self._log("global", "No valid diffs found in LLM output (JSON format).")
            return []
        self._log("global", f"Parsed JSON diff for {len(parsed_json)} files: {list(parsed_json.keys())}")
        return _apply_patch(package_path, parsed_json)

    # -------------------------------------------------------------------
    # Docker build backend (aligned with Build-bench docker.py)
    # -------------------------------------------------------------------

    def _resolve_docker_case(self, package_name: str, workspace: Path) -> Path:
        """解析 canonical case 路径，对齐 Build-bench DockerBuildBackend._resolve_case"""
        metadata = _load_workspace_metadata(workspace)
        candidates: list[Path] = []
        if metadata.get("source_case_dir"):
            candidates.append(Path(str(metadata["source_case_dir"])).expanduser())
        if package_name in self.case_map:
            candidates.append(self.case_map[package_name])
        if self.case_store_dir is not None:
            candidates.append(self.case_store_dir / package_name)
        # 兜底：config 的 case_store_dir 硬编码为默认方向；--base-dir 指向其他方向时
        # 以当前 base_dir 作为 case 池（case 实际就存放在 base_dir/<pkg>）
        base_dir_path = Path(self.base_dir).expanduser()
        if base_dir_path != self.case_store_dir:
            candidates.append(base_dir_path / package_name)

        for candidate in candidates:
            resolved = candidate.resolve()
            if (resolved / "manifest.json").is_file() and (resolved / "input").is_dir():
                return resolved
        raise RuntimeError(
            f"No canonical Docker Case found for {package_name!r}; "
            "add to build.docker.case_map or ensure case exists in case_store_dir"
        )

    def _docker_build_and_check(self, package_name: str, package_path: str) -> Tuple[bool, str]:
        """通过 Docker Validator 构建软件包，返回 (success, build_log_text)

        对齐 Build-bench DockerBuildBackend.build() 流程：
        1. 解析 canonical case（带 manifest.json/config/dependencies）
        2. 复制 case 到 staging 目录
        3. 将 Agent workspace 文件覆盖到 staging/input/
        4. 刷新校验和映射
        5. 运行 build-case-docker
        6. 解析 build-result.json
        """
        workspace = Path(package_path).resolve()
        try:
            source_case = self._resolve_docker_case(package_name, workspace)
        except (OSError, RuntimeError, ValueError) as error:
            self._log(package_name, f"Docker case resolve error: {error}")
            return False, str(error)

        stamp = time.strftime("%Y%m%d_%H%M%S")
        run_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        output_dir = (self.docker_result_root / package_name / run_id).resolve()
        staged_case = (self.docker_staging_root / package_name / run_id).resolve()
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staged_case.parent.mkdir(parents=True, exist_ok=True)

        try:
            _copy_case(source_case, staged_case)
            # 方案 A：先把 workspace 副本 extracted/ 的修改重压缩回归档，再复制进 staging
            recompress_msg = _recompress_workspace(str(workspace))
            if recompress_msg:
                self._log(package_name, f"Recompress: {recompress_msg}")
            _copy_workspace_input(workspace, staged_case / "input")
            _refresh_staged_source_map(staged_case)
            command = [
                *self.docker_validator_command,
                "--input",
                str(staged_case),
                "--output",
                str(output_dir),
                "--worker-mode", "isolated-chroot",
            ]
            self._log(package_name, f"Docker build command: {' '.join(command)}")
            completed = subprocess.run(command, check=False, text=True)

            result_file = output_dir / "build-result.json"
            if not result_file.is_file():
                return False, (
                    "Docker Validator did not produce build-result.json "
                    f"(exit code {completed.returncode})"
                )

            raw = json.loads(result_file.read_text(encoding="utf-8"))
            status = str(raw.get("status", "infrastructure_error"))
            log_path = output_dir / "build.log"
            success = status == "succeeded" and bool(
                raw.get("artifact_validation_passed", False)
            )

            # 构建失败时将 build.log 复制到 workspace/log_failed.txt 供下轮 LLM 分析
            build_log_text = ""
            if not success and log_path.is_file():
                shutil.copy2(log_path, workspace / "log_failed.txt")
            if log_path.is_file():
                build_log_text = log_path.read_text(encoding="utf-8", errors="replace")

            self._log(package_name, f"Docker build status: {status}, success: {success}")
            return success, build_log_text

        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
            self._log(package_name, f"Docker build error: {error}")
            return False, f"Docker Validator invocation failed: {error}"
        finally:
            shutil.rmtree(staged_case, ignore_errors=True)

    # -------------------------------------------------------------------
    # OBS build backend (fallback when backend=obs or no canonical case)
    # -------------------------------------------------------------------

    def _upload_and_check(self, package_name: str, package_path: str) -> Tuple[bool, str]:
        """OBS 构建：压缩 → 上传 → 轮询结果（原版逻辑，作为 Docker 的 fallback）"""
        from tools.auto_repair.upload_files import main_upload
        from tools.auto_repair.check_build_res import check_main

        has_spec = any(
            f.endswith(".spec")
            for f in os.listdir(package_path)
            if os.path.isfile(os.path.join(package_path, f))
        )
        if not has_spec:
            return False, f"Error: No .spec file in '{package_path}'"

        try:
            upload_result = main_upload(package_name, package_path)
            self._log(package_name, f"Upload: {upload_result}")
        except Exception as e:
            return False, f"Upload error: {e}"

        try:
            build_log = check_main(package_path, package_name)
            self._log(package_name, f"Build result: {build_log}")
            success = "success" in build_log.lower() or "succeeded" in build_log.lower()
            return success, build_log
        except Exception as e:
            return False, f"Build check error: {e}"

    def _build_and_check(self, package_name: str, package_path: str) -> Tuple[bool, str]:
        """统一的构建入口：Docker 构建，找不到 canonical case 时报错（不再 fallback OBS）"""
        if self.build_backend != "docker":
            raise RuntimeError(
                f"Unsupported build backend: {self.build_backend!r}. "
                "Only 'docker' is supported.")
        return self._docker_build_and_check(package_name, package_path)

    def process_all_packages(self):
        packages = self._get_packages_to_process()
        if not packages:
            self._log("global", "No packages to process.")
            return

        prompt_file = MODE_PROMPT_MAP.get(self.mode, "prompts/patch_generation_json.txt")
        self._log("global", f"Loading prompt from {prompt_file} (mode={self.mode})")
        with open(prompt_file, "r", encoding="utf-8") as f:
            system_prompt_tpl = f.read()

        for idx, pkg in enumerate(packages, 1):
            self._log("global", f"\n=== [{idx}/{len(packages)}] {pkg} ===")
            try:
                self.process_one_package(pkg, system_prompt_tpl)
            except Exception as e:
                self._log(pkg, f"Fatal error: {e}\n{traceback.format_exc()}")

    def process_one_package(self, package_name: str, system_prompt_tpl: str):
        env = self._init_package_environment(package_name)
        if not env:
            return
        package_path = env["package_path"]
        result_file = env["result_file"]

        # 创建实验目录，后续 diff 直接保存到此处
        direction = self._get_migration_direction(package_name)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        exp_dir = os.path.join(self.experiments_root, direction, self.mode,
                               f"{package_name}_{timestamp}")
        os.makedirs(exp_dir, exist_ok=True)

        build_succeeded = False
        final_text = ""

        # ===== 预构建阶段：先对原始 case 构建一轮，获取初始失败日志 =====
        # 仅在 case 未自带失败日志（logs/docker-target-build.log）时执行：
        # - 构建成功 → case 本就不需要修复，跳过全部 LLM attempt
        # - 构建失败 → _docker_build_and_check 已把 build.log 复制为
        #               workspace/log_failed.txt，attempt 1 得到有信息量的初始失败日志
        shipped_log = os.path.join(package_path, "log_failed.txt")
        if self.prebuild:
            if os.path.exists(shipped_log):
                self._log(package_name, "Case ships an initial failure log — skipping pre-build.")
            else:
                self._log(package_name,
                          "--- Pre-build: pristine case, no shipped failure log ---")
                prebuild_start = time.time()
                prebuild_ok, _ = self._build_and_check(package_name, package_path)
                prebuild_time = time.time() - prebuild_start
                self.timing_info[package_name] = {"prebuild": prebuild_time}
                if prebuild_ok:
                    self._log(package_name,
                              f"Pre-build SUCCEEDED on pristine case ({prebuild_time:.1f}s) — "
                              "case builds out of the box, no repair needed.")
                    build_succeeded = True
                    final_text = (f"Pre-build succeeded without any modification.\n"
                                  f"The case builds successfully as-is ({prebuild_time:.1f}s).")
                else:
                    self._log(package_name,
                              f"Pre-build FAILED ({prebuild_time:.1f}s). Initial failure log "
                              "captured; attempt 1 will be informed.")

        # 预构建成功 → 无需修复，直接记录结果并返回
        if build_succeeded:
            with open(result_file, "w", encoding="utf-8") as f:
                f.write(final_text)
            self._log(package_name, f"Result saved to {result_file}")
            self._log(package_name, "Skipping LLM attempts (case already builds).")
            self._save_experiment_record(package_name, build_succeeded, final_text,
                                         sum(self.timing_info.get(package_name, {}).values()),
                                         exp_dir)
            return

        for attempt in range(1, self.max_build_attempts + 1):
            self._log(package_name, f"--- Build attempt {attempt}/{self.max_build_attempts} ---")
            attempt_start = time.time()

            # 每轮重新解压，确保 LLM 能看到最新源码
            _extract_archive(package_path)

            # 构建 system prompt
            system_prompt = system_prompt_tpl.replace(
                "__TEMP_DIR__", package_path
            ).replace(
                "__FILE_NAME__", os.path.join(self.result_dir, f"{package_name}_result.txt")
            )

            # ===== 收集所有信息，一次性注入给 LLM =====
            dir_tree = self._get_directory_tree(package_path)
            build_log = self._get_build_log(package_path)
            history_section = self._build_history_section(package_name, attempt)
            pkg_files = _collect_package_files(package_path, max_chars=256000,
                                                   log_func=lambda msg: self._log("global", msg))

            # 构建 user content（不含 READ: 指令 — 纯 LLM 模式）
            user_content = (
                f"Please analyze and repair package {package_name} in: {package_path}. "
                f"All modifications must be done in the temporary directory."
            )
            user_content += f"\n\n## Directory structure\n{dir_tree}"
            if pkg_files:
                user_content += f"\n\n## Source files\n"
                user_content += self._build_source_section(pkg_files)
            if build_log:
                user_content += f"\n\n## Build failure log (log_failed.txt)\n```\n{build_log}\n```"
            if history_section:
                user_content += history_section
            user_content += (
                "\n\n## Instructions\n"
                "Refer to the system prompt for the exact output format and unified diff specification."
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]

            # ===== 单次 LLM 调用（无 READ 循环、无格式重试） =====
            self._log(package_name, f"Attempt {attempt}: Calling LLM (single shot)...")
            llm_failed = False
            try:
                # 纯 prompt + JSON 模板约束输出（不传 response_format / tools，
                # 云 API 上 json_schema / json_object 均实测 502，详见 _ollama_chat）
                llm_text = _ollama_chat(
                    model=self.llm_cfg.model,
                    messages=messages,
                    max_tokens=16384,
                    meta={
                        "package": package_name,
                        "attempt": attempt,
                        "mode": self.mode,
                        "model": self.llm_cfg.model,
                    },
                )
                # 剥离 thinking 标签
                llm_text = _strip_thinking(llm_text)
                self._log(package_name, f"LLM response: {len(llm_text)} chars")
            except Exception as e:
                self._log(package_name, f"LLM call failed: {e}")
                llm_failed = True
                final_text = f"LLM call failed on attempt {attempt}: {e}"

            # 记录 LLM 推理耗时（仅 LLM 调用，不包含构建）
            llm_time = time.time() - attempt_start
            self._log(package_name, f"LLM time: {llm_time:.1f}s")

            if llm_failed:
                # LLM 失败，只有 LLM 时间，无构建
                if package_name not in self.timing_info:
                    self.timing_info[package_name] = {}
                self.timing_info[package_name][attempt] = {
                    "llm_time_seconds": round(llm_time, 1),
                    "build_time_seconds": None,
                    "time_seconds": round(llm_time, 1),
                }
                continue

            # 保存原始 LLM 输出到调试目录
            debug_path = os.path.join(self.debug_dir,
                f"{package_name}_attempt{attempt}.json")
            try:
                with open(debug_path, "w", encoding="utf-8") as df:
                    df.write(llm_text)
            except Exception:
                pass

            # ===== 解析输出（一次，不重试） =====
            modified = []
            parse_error = None

            try:
                modified = self._parse_and_apply_patch(package_path, llm_text)
            except json.JSONDecodeError as e:
                parse_error = f"JSON parse error: {e}"
            except Exception as e:
                parse_error = f"Parse error: {e}"

            if parse_error or not modified:
                # 确定失败原因
                if parse_error:
                    fail_reason = "FORMAT_ERROR"
                    detail = parse_error
                elif not modified:
                    # JSON 合法但应用失败 — 检查 diffs 是否为空还是路径/上下文问题
                    try:
                        data = json.loads(llm_text)
                        diffs = data.get("diffs", [])
                        if not diffs:
                            fail_reason = "NO_DIFFS"
                            detail = "diffs array was empty"
                        else:
                            fail_reason = "APPLY_ERROR"
                            detail = f"none of {len(diffs)} diff(s) could be applied"
                    except Exception:
                        fail_reason = "FORMAT_ERROR"
                        detail = "JSON parseable but format unexpected"

                self._log(package_name, f"Attempt {attempt}: {fail_reason} — {detail}")

                # 记录失败到修改历史（供下一轮 attempt 参考）
                if package_name not in self.modification_history:
                    self.modification_history[package_name] = []
                self.modification_history[package_name].append({
                    "file_path": fail_reason,
                    "diff": [],
                    "build_result": fail_reason,
                    "llm_output_preview": llm_text[:2000],
                })

                final_text = f"Attempt {attempt}: {fail_reason} — {detail}"
                # 解析/应用失败，记录 timing（LLM 时间 + 无构建）
                if package_name not in self.timing_info:
                    self.timing_info[package_name] = {}
                self.timing_info[package_name][attempt] = {
                    "llm_time_seconds": round(llm_time, 1),
                    "build_time_seconds": None,
                    "time_seconds": round(time.time() - attempt_start, 1),
                }
                continue

            # ===== 应用成功，构建检查 =====
            self._log(package_name, f"Applied patch to {len(modified)} files: {modified}")

            # 从 LLM JSON 中提取原始 diff 文本（用于保存和修改历史）
            raw_diffs = {}
            try:
                data = json.loads(llm_text)
                for entry in data.get("diffs", []):
                    p = entry.get("path", "")
                    d = entry.get("diff", "")
                    if p and d:
                        raw_diffs[p] = d
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

            # 保存 repair.diff
            diff_path = os.path.join(exp_dir,
                f"attempt{attempt}.diff")
            try:
                with open(diff_path, "w", encoding="utf-8") as df:
                    for fpath, diff_text in raw_diffs.items():
                        df.write(f"# File: {fpath}\n")
                        df.write(diff_text)
                        df.write("\n")
                self._log(package_name, f"Repair diff saved to {diff_path}")
            except Exception as e:
                self._log(package_name, f"Failed to save diff: {e}")

            # 重新压缩（仅 OBS 模式需要）
            if self.build_backend == "obs" and os.path.exists(os.path.join(package_path, "extracted")):
                compress_result = _compress_archive(package_path)
                self._log(package_name, f"Compress: {compress_result}")

            # 构建检查（Docker 或 OBS）
            build_success, build_log = self._build_and_check(package_name, package_path)
            build_result = "SUCCESS" if build_success else "FAILED"

            # 记录修改历史
            if package_name not in self.modification_history:
                self.modification_history[package_name] = []
            for fpath in modified:
                diff_text = raw_diffs.get(fpath, "")
                line_changes = _unified_diff_to_line_ops(diff_text) if diff_text else []
                self.modification_history[package_name].append({
                    "file_path": fpath,
                    "diff": line_changes,
                    "build_result": build_result,
                })

            if build_success:
                # 记录完整 attempt 耗时（LLM + 构建）
                attempt_total_time = time.time() - attempt_start
                build_time = attempt_total_time - llm_time
                if package_name not in self.timing_info:
                    self.timing_info[package_name] = {}
                self.timing_info[package_name][attempt] = {
                    "llm_time_seconds": round(llm_time, 1),
                    "build_time_seconds": round(build_time, 1),
                    "time_seconds": round(attempt_total_time, 1),
                }
                self._log(package_name, f"Attempt {attempt} total time: {attempt_total_time:.1f}s (LLM: {llm_time:.1f}s, build: {build_time:.1f}s)")

                build_succeeded = True
                final_text = (f"Build succeeded on attempt {attempt}.\n"
                              f"Files modified: {modified}\nBuild result: {build_result}")
                self._log(package_name, f"BUILD SUCCESS on attempt {attempt}!")
                break
            else:
                final_text = (f"Build failed on attempt {attempt}.\n"
                              f"Files modified: {modified}\nBuild result: {build_result}")
                self._log(package_name, f"Build failed on attempt {attempt}: {build_log[:500]}")

                # 记录完整 attempt 耗时（LLM + 构建）
                attempt_total_time = time.time() - attempt_start
                build_time = attempt_total_time - llm_time
                if package_name not in self.timing_info:
                    self.timing_info[package_name] = {}
                self.timing_info[package_name][attempt] = {
                    "llm_time_seconds": round(llm_time, 1),
                    "build_time_seconds": round(build_time, 1),
                    "time_seconds": round(attempt_total_time, 1),
                }
                self._log(package_name, f"Attempt {attempt} total time: {attempt_total_time:.1f}s (LLM: {llm_time:.1f}s, build: {build_time:.1f}s)")

        # 保存最终结果
        with open(result_file, "w", encoding="utf-8") as f:
            f.write(final_text)
        self._log(package_name, f"Result saved to {result_file}")
        if not build_succeeded:
            self._log(package_name, "Max attempts reached without success.")

        # 时间统计
        timing_dict = self.timing_info.get(package_name, {})
        total_time = 0
        for k, v in timing_dict.items():
            if k == "prebuild":
                total_time += v
            else:
                total_time += v.get("time_seconds", 0) if isinstance(v, dict) else v
        num_attempts = sum(1 for k in timing_dict if isinstance(k, int))
        avg_time = total_time / num_attempts if num_attempts else 0
        if timing_dict:
            time_list = [timing_dict.get(i) for i in range(1, self.max_build_attempts + 1)]
            self._log(package_name, f"Timing summary: {time_list}")
            self._log(package_name, f"Total time: {total_time:.1f}s, Avg/attempt: {avg_time:.1f}s")

        # ===== 保存实验结果到 experiments 目录 =====
        self._save_experiment_record(package_name, build_succeeded, final_text, total_time, exp_dir)

    def _get_migration_direction(self, package_name: str) -> str:
        """从 base_dir 路径推断迁移方向"""
        base = os.path.basename(os.path.normpath(self.base_dir))
        return base  # 如 aarch64_to_x86_64

    def _save_experiment_record(self, package_name: str, build_succeeded: bool,
                                 final_text: str, total_time: float, exp_dir: str):
        """保存实验结果 summary.json 到指定实验目录（diffs 已在尝试循环中直接写入）"""
        direction = self._get_migration_direction(package_name)
        timestamp = os.path.basename(exp_dir).split("_", 1)[1] if "_" in os.path.basename(exp_dir) else time.strftime("%Y%m%d_%H%M%S")

        # 收集 attempt 信息
        attempts = []
        timing_dict = self.timing_info.get(package_name, {})  # {attempt: dict} (+"prebuild": float)
        history = self.modification_history.get(package_name, [])
        attempt_timings = {k: v for k, v in timing_dict.items() if isinstance(k, int)}

        # 预构建成功（未进入 attempt 循环）时不生成误导的 attempts 条目
        if history or attempt_timings:
            for i in range(1, self.max_build_attempts + 1):
                attempt_timing = attempt_timings.get(i)
                attempt_result = "FAILED"
                if i <= len(history):
                    h = history[i-1]
                    br = h.get("build_result", "FAILED")
                    if br == "SUCCESS":
                        attempt_result = "SUCCESS"
                    elif br in ("FORMAT_ERROR", "NO_DIFFS", "APPLY_ERROR"):
                        attempt_result = br

                entry = {
                    "attempt": i,
                    "result": attempt_result,
                }
                if attempt_timing is not None:
                    if isinstance(attempt_timing, dict):
                        entry["time_seconds"] = attempt_timing.get("time_seconds")
                        if attempt_timing.get("llm_time_seconds") is not None:
                            entry["llm_time_seconds"] = attempt_timing["llm_time_seconds"]
                        if attempt_timing.get("build_time_seconds") is not None:
                            entry["build_time_seconds"] = attempt_timing["build_time_seconds"]
                    else:
                        # 兼容旧格式（float）
                        entry["time_seconds"] = round(attempt_timing, 1)

                # 添加 token 消耗信息
                token_key = f"{package_name}_{i}_{self.mode}"
                if token_key in _TOKEN_USAGE_BY_ATTEMPT:
                    entry["token_usage"] = _TOKEN_USAGE_BY_ATTEMPT[token_key]

                attempts.append(entry)

        summary = {
            "package": package_name,
            "direction": direction,
            "mode": self.mode,
            "model": self.llm_cfg.model,
            "backend": self.build_backend,
            "prebuild": bool(timing_dict.get("prebuild")),
            "total_time_seconds": round(total_time, 1),
            "final_result": "SUCCESS" if build_succeeded else "FAILED",
            "attempts": attempts,
            "timestamp": timestamp,
        }

        summary_path = os.path.join(exp_dir, "summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        self._log(package_name, f"Experiment record saved to {summary_path}")

        # 复制运行日志（result_log_res/{package_name}.log）到实验目录
        log_src = os.path.join(self.log_dir, f"{package_name}.log")
        if os.path.isfile(log_src):
            shutil.copy2(log_src, os.path.join(exp_dir, "run.log"))
            self._log(package_name, f"Run log copied to {exp_dir}/run.log")


def main():
    provider = info["LLM_PROVIDER"].lower()
    default_model = {
        "openai": "gpt-5",
        "qwen": os.getenv("LLM_MODEL", "qwen3-max"),
        "claude": "claude-sonnet-4-5-20250929",
        "deepseek": "deepseek-v3",
    }.get(provider)

    llm_cfg = LLMConfig(provider=provider, model=default_model)
    baseline = AutoRepairBaselinePatch(llm=llm_cfg)
    baseline.process_all_packages()


if __name__ == "__main__":
    main()
