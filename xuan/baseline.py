"""baseline.py — 纯 LLM 推理基线（无 MCP，无 function calling tools）

LLM 不调用任何工具，只做纯推理输出修复方案。
框架负责所有基础设施：读文件、写文件、构建检查（Docker 或 OBS）。
"""

import os
import re
import json
import shutil
import shlex
import time
import traceback
import hashlib
import subprocess
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import yaml
from dotenv import load_dotenv
from openai import OpenAI

from baseline_patch import (
    _collect_package_files,
    _unified_diff_to_line_ops,
    _copy_case,
    _copy_workspace_input,
    _refresh_staged_source_map,
    _load_workspace_metadata,
    _extract_archive,
    _compress_archive,
    _recompress_workspace,
    _is_text_file,
    _strip_thinking,
    _resolve_package_path,
    _ollama_chat,
    _truncate_build_log,
    _TOKEN_USAGE_BY_ATTEMPT,
)

load_dotenv(".env")
with open("config/info.yaml", "r") as f:
    info = yaml.safe_load(f)


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
# AutoRepairBaseline — 纯 LLM 基线
# ---------------------------------------------------------------------------

class AutoRepairBaseline:
    def __init__(
        self,
        llm: Optional[LLMConfig] = None,
        base_dir: Optional[str] = None,
        max_build_attempts: int = 3,
        prebuild: Optional[bool] = None,
        *,
        mode: str = "full_file",
        # 并行执行时覆盖的路径参数
        result_dir: Optional[str] = None,
        log_dir: Optional[str] = None,
        temp_work_dir: Optional[str] = None,
        docker_result_root: Optional[str] = None,
        docker_staging_root: Optional[str] = None,
    ) -> None:
        self.llm_cfg = llm
        self.base_dir = base_dir or info["paths"]["base_dir"]
        self.result_dir = result_dir or info["paths"]["result_dir"]
        self.log_dir = log_dir or info["paths"]["log_dir"]
        self.temp_work_dir = temp_work_dir or info["paths"]["temp_work_dir"]
        self.max_build_attempts = max_build_attempts

        os.makedirs(self.result_dir, exist_ok=True)
        os.makedirs(self.temp_work_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)

        # 实验数据收集目录（保存在 temp-guidance，不污染 data/）
        base_dir_path = os.path.dirname(os.path.abspath(__file__))
        guidance_dir = os.path.join(base_dir_path, "..", "temp-guidance")
        self.debug_dir = os.path.join(guidance_dir, "llm_debug")
        self.experiments_root = os.path.join(guidance_dir, "experiments")
        self.mode = mode  # 区分实验模式（full_file / spec 等）

        # 预构建开关：参数优先，其次 config/build.prebuild，默认开启
        build_cfg = info.get("build") or {}
        self.prebuild = build_cfg.get("prebuild", True) if prebuild is None else prebuild
        os.makedirs(self.debug_dir, exist_ok=True)
        os.makedirs(self.experiments_root, exist_ok=True)

        # 修改历史：{package_name: [history_entry, ...]}
        self.modification_history: Dict[str, List[Dict]] = {}
        self.timing_info: Dict[str, List[float]] = {}  # {package_name: [attempt1_time, ...]}

        # --- Docker build backend config (aligned with baseline_patch) ---
        build_cfg = info.get("build") or {}
        self.build_backend = os.getenv("BUILD_BENCH_BACKEND",
                                       build_cfg.get("backend", "obs")).strip().lower()
        docker_cfg = dict(build_cfg.get("docker") or {})
        self.docker_validator_command = shlex.split(str(
            docker_cfg.get("validator_command",
                           "/home/zhaochenyu/buildbench_competition/docker-validator/bin/build-case-docker")
        ))
        self.docker_result_root = Path(
            docker_result_root or docker_cfg.get("result_root", "run_records/docker-builds")
        ).expanduser()
        self.docker_staging_root = Path(
            docker_staging_root or docker_cfg.get("staging_root", "temp_workspace/docker-cases")
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

    # ----- 框架基础设施 -----

    def _get_packages_to_process(self) -> List[str]:
        """获取待处理包列表"""
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
        """复制包到临时工作目录，初始化环境"""
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
        """读取构建失败日志，如果不存在则返回空字符串"""
        log_path = os.path.join(package_path, "log_failed.txt")
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
        return ""

    def _build_source_section(self, files: Dict[str, str]) -> str:
        """将源文件拼接为 prompt 中的代码块"""
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

    def _parse_llm_output_files(self, text: str) -> Dict[str, str]:
        """解析 LLM 输出的 JSON，提取 files 数组，返回 {path: content}"""
        try:
            data = json.loads(text.strip())
        except (json.JSONDecodeError, TypeError, AttributeError):
            return {}
        if "files" not in data or not isinstance(data["files"], list):
            return {}
        result = {}
        for entry in data["files"]:
            path = entry.get("path", "").strip()
            content = entry.get("content", "")
            if path and content:
                result[path] = content
        # 过滤 "No changes" 哨兵
        result = {k: v for k, v in result.items()
                  if not (k == "README" and v.strip() == "No changes")}
        return result

    def _apply_file_replacements(self, package_path: str,
                                  file_changes: Dict[str, str]) -> List[str]:
        """将全文件替换应用到包目录，返回修改的文件列表

        对齐原版 MCP 的 modify_file_tool：读取旧内容 → 写新内容 → 记录行级 diff。
        """
        import difflib
        modified = []
        for rel_path, new_content in file_changes.items():
            full_path = _resolve_package_path(package_path, rel_path)
            if not os.path.exists(full_path):
                self._log("global", f"Skipped (file does not exist): {full_path}")
                continue
            # 读取旧内容用于 diff
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    old_content = f.read()
            except Exception:
                old_content = ""

            with open(full_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            modified.append(rel_path)
            self._log("global", f"Written: {full_path} ({len(new_content)} chars)")

            # 记录行级 diff 到 modification_history 供下一轮使用
            pkg_name = os.path.basename(package_path)
            diff_text = "".join(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                lineterm="",
            ))
            if pkg_name not in self.modification_history:
                self.modification_history[pkg_name] = []
            self.modification_history[pkg_name].append({
                "file_path": rel_path,
                "diff": _unified_diff_to_line_ops(diff_text),
                "raw_diff": diff_text,          # 保存原始 unified diff 供导出
                "build_result": "PENDING",
            })

        return modified

    # -------------------------------------------------------------------
    # Docker build backend (aligned with baseline_patch)
    # -------------------------------------------------------------------

    def _resolve_docker_case(self, package_name: str, workspace: Path) -> Path:
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
                "--input", str(staged_case),
                "--output", str(output_dir),
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
    # OBS build backend (fallback)
    # -------------------------------------------------------------------

    def _upload_and_check(self, package_name: str, package_path: str) -> Tuple[bool, str]:
        """OBS 构建（原版逻辑，作为 Docker 的 fallback）"""
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

    # ----- 主流程 -----

    def process_all_packages(self):
        packages = self._get_packages_to_process()
        if not packages:
            self._log("global", "No packages to process.")
            return

        with open("prompts/full_file_generation_json.txt", "r", encoding="utf-8") as f:
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

        # 创建实验目录，后续修复记录直接保存到此处
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
                "Refer to the system prompt for the exact JSON format. "
                "Output the COMPLETE file content, not just changed lines."
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

            # 记录当前 modification_history 长度，用于后续提取本次 diff
            _pre_hist_len = len(self.modification_history.get(package_name, []))

            try:
                file_changes = self._parse_llm_output_files(llm_text)
                if file_changes:
                    modified = self._apply_file_replacements(package_path, file_changes)
            except json.JSONDecodeError as e:
                parse_error = f"JSON parse error: {e}"
            except Exception as e:
                parse_error = f"Parse error: {e}"

            if parse_error or not modified:
                if parse_error:
                    fail_reason = "FORMAT_ERROR"
                    detail = parse_error
                elif not modified:
                    try:
                        data = json.loads(llm_text)
                        files_arr = data.get("files", [])
                        if not files_arr:
                            fail_reason = "NO_FILES"
                            detail = "files array was empty"
                        else:
                            fail_reason = "APPLY_ERROR"
                            detail = f"none of {len(files_arr)} file(s) could be written"
                    except Exception:
                        fail_reason = "FORMAT_ERROR"
                        detail = "JSON parseable but format unexpected"

                self._log(package_name, f"Attempt {attempt}: {fail_reason} — {detail}")

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
            self._log(package_name, f"Applied full file replacements to {len(modified)} files: {modified}")

            # 从 LLM JSON 中提取原始 content（用于保存）
            raw_contents = {}
            try:
                data = json.loads(llm_text)
                for entry in data.get("files", []):
                    p = entry.get("path", "")
                    c = entry.get("content", "")
                    if p and c:
                        raw_contents[p] = c
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

            # 保存修复摘要
            diff_path = os.path.join(exp_dir,
                f"attempt{attempt}.txt")
            try:
                with open(diff_path, "w", encoding="utf-8") as df:
                    for fpath, content in raw_contents.items():
                        df.write(f"# File: {fpath} (full replacement)\n")
                        df.write(f"# New content: {len(content)} chars\n")
                        df.write(content[:5000])
                        df.write("\n")
                self._log(package_name, f"Repair diff saved to {diff_path}")
            except Exception as e:
                self._log(package_name, f"Failed to save diff: {e}")

            # 保存 unified diff（repair.diff）到实验目录
            repair_diff_path = os.path.join(exp_dir,
                f"attempt{attempt}.diff")
            try:
                hist = self.modification_history.get(package_name, [])
                new_entries = hist[_pre_hist_len:]
                with open(repair_diff_path, "w", encoding="utf-8") as rf:
                    for entry in new_entries:
                        rd = entry.get("raw_diff", "")
                        if rd:
                            rf.write(rd)
                            rf.write("\n")
                self._log(package_name, f"Unified diff saved to {repair_diff_path}")
            except Exception as e:
                self._log(package_name, f"Failed to save repair.diff: {e}")

            # 重新压缩（仅 OBS 模式需要）
            if self.build_backend == "obs" and os.path.exists(os.path.join(package_path, "extracted")):
                compress_result = _compress_archive(package_path)
                self._log(package_name, f"Compress: {compress_result}")

            # 构建检查（Docker 或 OBS）
            build_success, build_log = self._build_and_check(package_name, package_path)
            build_result = "SUCCESS" if build_success else "FAILED"

            # 更新 modification_history 中的 build_result（_apply_file_replacements 已记录 diff）
            if package_name in self.modification_history:
                for entry in self.modification_history[package_name]:
                    if entry.get("build_result") == "PENDING":
                        entry["build_result"] = build_result

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
        """保存实验结果 summary.json 到指定实验目录（修复记录已在循环中直接写入）"""
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    provider = info["LLM_PROVIDER"].lower()
    default_model = {
        "openai": "gpt-5",
        "qwen": os.getenv("LLM_MODEL", "qwen3-max"),
        "claude": "claude-sonnet-4-5-20250929",
        "deepseek": "deepseek-v3",
    }.get(provider)

    llm_cfg = LLMConfig(provider=provider, model=default_model)
    baseline = AutoRepairBaseline(llm=llm_cfg)
    baseline.process_all_packages()


if __name__ == "__main__":
    main()
