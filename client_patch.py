import asyncio
import os
import json
import time
import traceback
import sys
from typing import Any, Dict, List, Optional, Tuple
from contextlib import AsyncExitStack
import yaml
from dotenv import load_dotenv
from openai import OpenAI
from mcp.client.stdio import stdio_client
from mcp import ClientSession, StdioServerParameters
from tools.runtime_config import (
    load_agent_config,
    resolve_agent_config_path,
)

load_dotenv(".env")


class LLMConfig:
    """
    Unified config so we can swap providers/models without changing client logic.

    Priority for credentials per provider:
      - OPENAI_* for native OpenAI (e.g., gpt-5-mini)
      - DASHSCOPE_* for Qwen (DashScope)
      - CHATANYWHERE_* for Claude (OpenAI-compatible facade)

    You can also override via explicit arguments when constructing AutoRepairClientUnified.
    """
    def __init__(
        self,
        provider: str,  # "openai", "qwen", or "claude"
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
            raise RuntimeError(f"Missing API key for provider={self.provider}. Check your environment.")

    def make_client(self) -> OpenAI:
        if self.base_url:
            return OpenAI(api_key=self.api_key, base_url=self.base_url)
        return OpenAI(api_key=self.api_key)


def make_args_key(tool_name: str, tool_args: dict) -> str:
    return f"{tool_name}::{json.dumps(tool_args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))}"


def parse_modification_tool_result(
    tool_args: Dict[str, Any],
    tool_ret: str,
) -> Dict[str, Any]:
    """Parse one apply_git_unified_patch_tool result.

    Supports the current text protocol and a future structured JSON
    response.  Unified-diff state and actual file modification state are
    deliberately recorded as separate concepts.
    """
    mode = str(tool_args.get("mode") or "unified_diff")
    text = str(tool_ret or "").strip()
    lines = text.splitlines()
    first_line = lines[0].strip() if lines else ""

    event: Dict[str, Any] = {
        "success": False,
        "operation": mode,
        "modified_paths": [],
        "replacement_count": None,
        "unified_diff_supplied": (
            mode == "unified_diff"
            and bool(str(tool_args.get("patch_text") or "").strip())
        ),
        "unified_diff_applied": False,
        "raw_status": first_line,
    }

    def add_path(raw_path: Any) -> None:
        path = str(raw_path or "").strip()
        if path and path not in event["modified_paths"]:
            event["modified_paths"].append(path)

    parsed: Optional[Dict[str, Any]] = None

    try:
        candidate = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        candidate = None

    if isinstance(candidate, dict) and "success" in candidate:
        parsed = candidate
        event["success"] = bool(parsed.get("success"))
        event["operation"] = str(
            parsed.get("operation") or mode
        )

        raw_paths = parsed.get("modified_paths")

        if isinstance(raw_paths, list):
            for path in raw_paths:
                add_path(path)
        elif isinstance(raw_paths, str):
            add_path(raw_paths)

        add_path(parsed.get("modified_path"))

        replacement_count = parsed.get("replacement_count")
        if replacement_count is not None:
            try:
                event["replacement_count"] = int(replacement_count)
            except (TypeError, ValueError):
                event["replacement_count"] = None
    else:
        if first_line == "Success: exact_replace":
            event["success"] = True
            event["operation"] = "exact_replace"
        elif first_line == "Success: applied patch":
            event["success"] = True
            event["operation"] = "unified_diff"

        for line in lines[1:]:
            stripped = line.strip()

            if (
                len(stripped) > 2
                and stripped[0] in {"A", "M", "D"}
                and stripped[1] == " "
            ):
                add_path(stripped[2:])
            elif stripped.startswith("replacements="):
                try:
                    event["replacement_count"] = int(
                        stripped.split("=", 1)[1]
                    )
                except (IndexError, ValueError):
                    event["replacement_count"] = None

    if (
        event["success"]
        and not event["modified_paths"]
        and event["operation"] == "exact_replace"
    ):
        add_path(tool_args.get("file_path"))

    if event["operation"] == "git_unified_patch":
        event["operation"] = "unified_diff"

    event["unified_diff_applied"] = bool(
        event["success"]
        and event["operation"] == "unified_diff"
    )

    return event


def merge_modification_event(
    target: Dict[str, Any],
    event: Dict[str, Any],
) -> None:
    """Accumulate one modification event without overwriting prior success."""
    target["patch_generated"] = bool(
        target.get("patch_generated")
        or event.get("unified_diff_supplied")
    )
    target["patch_applied"] = bool(
        target.get("patch_applied")
        or event.get("unified_diff_applied")
    )
    target["unified_diff_supplied"] = bool(
        target.get("unified_diff_supplied")
        or event.get("unified_diff_supplied")
    )
    target["unified_diff_applied"] = bool(
        target.get("unified_diff_applied")
        or event.get("unified_diff_applied")
    )
    target["modification_applied"] = bool(
        target.get("modification_applied")
        or event.get("success")
    )

    paths = target.setdefault("modified_paths", [])

    for path in event.get("modified_paths", []):
        if path not in paths:
            paths.append(path)

    target.setdefault("modification_events", []).append(dict(event))


class AutoRepairClientPatch:
    """
      - Connects to MCP server
      - Lists tools (OpenAI function-call format)
      - Iterates packages; for each package, performs N build attempts
      - Within each attempt, runs an LLM<->Tools loop handling tool calls
      - Features from both originals:
          * repeat-call guard + caching of tool results
          * upload-before-check-build enforcement
          * fallback auto-upload if model doesn't upload
    """

    def __init__(
        self,
        llm: Optional[LLMConfig] = None,
        base_dir: Optional[str] = None,
        server_script: str = "server_patch.py",
        max_retries: int = 2,
        max_build_attempts: int = 3,
        max_tool_rounds: int = 20,
        config: Optional[Dict[str, Any]] = None,
        config_path: Optional[str] = None,
    ) -> None:
        self.exit_stack = AsyncExitStack()
        self.session: Optional[ClientSession] = None
        self.is_session_active = False

        # One resolved configuration is shared with the
        # MCP Server process through its inherited environment.
        runtime_config_path = resolve_agent_config_path(
            config_path
        )
        self.config_path = str(runtime_config_path)
        self.info = (
            dict(config)
            if config is not None
            else load_agent_config(runtime_config_path)
        )
        os.environ[
            "BB_AGENT_CONFIG"
        ] = self.config_path

        # LLM adapter
        self.llm_cfg = llm
        self.client = self.llm_cfg.make_client()

        # Paths (choose sensible default if not given)
        self.base_dir = base_dir or self.info["paths"]["base_dir"]
        self.result_dir = self.info["paths"]["result_dir"]
        self.log_dir = self.info["paths"]["log_dir"]
        self.temp_work_dir = self.info["paths"]["temp_work_dir"]

        os.makedirs(self.result_dir, exist_ok=True)
        os.makedirs(self.temp_work_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)

        self.server_script = server_script
        self.max_retries = max_retries
        self.max_build_attempts = max_build_attempts
        self.max_tool_rounds = max_tool_rounds
        self.build_tool_timeout_seconds = int(
            (self.info.get("build") or {}).get("client_tool_timeout_seconds", 7200)
        )

        # Per-package state
        self.upload_status: Dict[str, bool] = {}
        self.active_experiment: Optional[Dict[str, Any]] = None
        self._active_attempt_started_monotonic: Optional[float] = None

    def _log(self, tag: str, msg: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        path = os.path.join(self.log_dir, f"{tag}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
        print(msg)

    def _start_experiment(self, package_name: str) -> Dict[str, Any]:
        experiment = {
            "schema_version": "1.2",
            "case_id": package_name,
            "experiment_group": os.getenv(
                "BUILD_BENCH_EXPERIMENT_GROUP", "unspecified"
            ),
            "final_status": "running",
            "stop_reason": None,
            "attempts_completed": 0,
            "tool_rounds": 0,
            "build_status": "not_run",
            "patch_generated": False,
            "patch_applied": False,
            "unified_diff_supplied": False,
            "unified_diff_applied": False,
            "modification_applied": False,
            "modified_paths": [],
            "modification_events": [],
            "attempts": [
                {
                    "attempt_index": attempt_index,
                    "status": "not_entered",
                    "started_at": None,
                    "finished_at": None,
                    "duration_seconds": 0.0,
                    "build_success": None,
                    "build_status": "not_run",
                    "model_call_count": 0,
                    "tool_rounds": 0,
                    "tool_call_count": 0,
                    "tool_execution_count": 0,
                    "build_call_count": 0,
                    "fallback_build_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "token_usage_reported": False,
                    "patch_generated": False,
                    "patch_applied": False,
                    "unified_diff_supplied": False,
                    "unified_diff_applied": False,
                    "modification_applied": False,
                    "modified_paths": [],
                    "modification_events": [],
                    "stop_reason": None,
                }
                for attempt_index in range(1, self.max_build_attempts + 1)
            ],
            "metrics_totals": {},
            "duration_seconds": 0.0,
            "exception_stage": "initialization",
            "started_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "finished_at": None,
            "_started_monotonic": time.monotonic(),
            "_result_file": None,
        }
        self.active_experiment = experiment
        self._active_attempt_started_monotonic = None
        return experiment

    def _active_attempt(
        self, package_name: str
    ) -> Optional[Dict[str, Any]]:
        experiment = self.active_experiment
        if not experiment or experiment.get("case_id") != package_name:
            return None
        for attempt in experiment["attempts"]:
            if attempt["status"] == "running":
                return attempt
        return None

    def _start_attempt(
        self, package_name: str, attempt_index: int
    ) -> Dict[str, Any]:
        experiment = self.active_experiment
        if not experiment or experiment.get("case_id") != package_name:
            raise RuntimeError("No active experiment for attempt metrics")
        attempt = experiment["attempts"][attempt_index - 1]
        attempt["status"] = "running"
        attempt["started_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        self._active_attempt_started_monotonic = time.monotonic()
        return attempt

    def _finish_attempt(
        self,
        package_name: str,
        status: str,
        build_success: Optional[bool],
        stop_reason: Optional[str],
    ) -> None:
        attempt = self._active_attempt(package_name)
        if attempt is None:
            return
        attempt["status"] = status
        attempt["build_success"] = build_success
        attempt["stop_reason"] = stop_reason
        attempt["finished_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        if self._active_attempt_started_monotonic is not None:
            attempt["duration_seconds"] = round(
                time.monotonic() - self._active_attempt_started_monotonic,
                3,
            )
        self._active_attempt_started_monotonic = None

    def _add_attempt_metric(
        self, package_name: str, metric: str, amount: int = 1
    ) -> None:
        attempt = self._active_attempt(package_name)
        if attempt is not None:
            attempt[metric] += amount

    def _record_token_usage(self, package_name: str, response: Any) -> None:
        attempt = self._active_attempt(package_name)
        usage = getattr(response, "usage", None)
        if attempt is None or usage is None:
            return

        def usage_value(*names: str) -> int:
            for name in names:
                value = (
                    usage.get(name)
                    if isinstance(usage, dict)
                    else getattr(usage, name, None)
                )
                if value is not None:
                    return int(value)
            return 0

        input_tokens = usage_value("prompt_tokens", "input_tokens")
        output_tokens = usage_value("completion_tokens", "output_tokens")
        total_tokens = usage_value("total_tokens")
        attempt["input_tokens"] += input_tokens
        attempt["output_tokens"] += output_tokens
        attempt["total_tokens"] += (
            total_tokens or input_tokens + output_tokens
        )
        attempt["token_usage_reported"] = True

    def _save_experiment(self, package_name: str) -> Optional[str]:
        experiment = self.active_experiment
        if not experiment or experiment.get("case_id") != package_name:
            return None

        experiment["duration_seconds"] = round(
            time.monotonic() - experiment["_started_monotonic"], 3
        )
        if experiment["final_status"] != "running":
            experiment["finished_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
        metric_names = (
            "model_call_count",
            "tool_rounds",
            "tool_call_count",
            "tool_execution_count",
            "build_call_count",
            "fallback_build_count",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        )
        experiment["metrics_totals"] = {
            name: sum(attempt[name] for attempt in experiment["attempts"])
            for name in metric_names
        }
        result_file = experiment.get("_result_file")
        if result_file:
            stem, _ = os.path.splitext(result_file)
            path = f"{stem}_experiment-result.json"
        else:
            path = os.path.join(
                self.result_dir,
                f"{package_name}_experiment-result.json",
            )

        public_result = {
            key: value
            for key, value in experiment.items()
            if not key.startswith("_")
        }
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(public_result, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
        self._log(package_name, f"Experiment result saved to {path}")
        return path


    def _write_agent_result(
        self,
        package_name: str,
        experiment_result_path: Optional[str] = None,
    ) -> Optional[str]:
        """Write the framework-owned terminal Agent result.

        The model does not write this file.  The Client derives it from
        the final experiment state after success, failure, or abort.
        """
        experiment = self.active_experiment
        if not experiment or experiment.get("case_id") != package_name:
            return None

        outcome = str(
            experiment.get("final_status") or "unknown"
        )
        stop_reason = experiment.get("stop_reason")

        if outcome == "user_aborted":
            status = "aborted"
        elif outcome in {"client_error", "running"}:
            status = "error"
        else:
            # "completed" means that the Agent invocation reached a
            # terminal result.  Repair success is represented separately
            # by the outcome field.
            status = "completed"

        if outcome == "success":
            message = "Verified build succeeded."
        else:
            message = (
                f"Agent finished with outcome={outcome}; "
                f"stop_reason={stop_reason or 'unspecified'}."
            )

        raw_modified_paths = experiment.get("modified_paths") or []
        modified_paths = sorted(
            {
                str(path)
                for path in raw_modified_paths
                if isinstance(path, str) and path.strip()
            }
        )

        experiment_reference = None

        if experiment_result_path:
            try:
                experiment_reference = os.path.relpath(
                    experiment_result_path,
                    self.result_dir,
                )
            except ValueError:
                experiment_reference = experiment_result_path

        payload = {
            "schema_version": "0.1",
            "status": status,
            "case_id": package_name,
            "outcome": outcome,
            "message": message,
            "modified_paths": modified_paths,
            "stop_reason": stop_reason,
            "experiment_result": experiment_reference,
            "generated_by": "client-framework",
        }

        output_dir = os.path.join(
            self.result_dir,
            package_name,
        )
        path = os.path.join(
            output_dir,
            "agent-result.json",
        )

        os.makedirs(output_dir, exist_ok=True)

        temporary_path = f"{path}.tmp"

        with open(
            temporary_path,
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")

        os.replace(temporary_path, path)

        self._log(
            package_name,
            f"Agent result saved to {path}",
        )

        official_workspace = str(
            os.getenv("BB_WORKSPACE") or ""
        ).strip()

        if official_workspace:
            official_output_dir = os.path.join(
                os.path.abspath(
                    os.path.expanduser(
                        official_workspace
                    )
                ),
                "output",
            )
            official_path = os.path.join(
                official_output_dir,
                "agent-result.json",
            )
            official_temporary_path = (
                f"{official_path}.tmp.{os.getpid()}"
            )

            try:
                os.makedirs(
                    official_output_dir,
                    exist_ok=True,
                )

                with open(
                    official_temporary_path,
                    "w",
                    encoding="utf-8",
                ) as handle:
                    json.dump(
                        payload,
                        handle,
                        ensure_ascii=False,
                        indent=2,
                    )
                    handle.write("\n")

                os.replace(
                    official_temporary_path,
                    official_path,
                )

                self._log(
                    package_name,
                    "Official Agent result saved to "
                    f"{official_path}",
                )
            except OSError as error:
                self._log(
                    package_name,
                    "Official Agent result mirror failed: "
                    f"{error}",
                )
            finally:
                try:
                    os.unlink(official_temporary_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

        return path

    def _finish_experiment(
        self,
        package_name: str,
        final_status: str,
        stop_reason: str,
        exception_stage: Optional[str] = None,
    ) -> None:
        experiment = self.active_experiment
        if not experiment or experiment.get("case_id") != package_name:
            return
        if self._active_attempt(package_name) is not None:
            attempt_status = (
                "aborted" if final_status == "user_aborted" else "error"
            )
            self._finish_attempt(
                package_name,
                attempt_status,
                False,
                stop_reason,
            )
        experiment["final_status"] = final_status
        experiment["stop_reason"] = stop_reason
        experiment["exception_stage"] = exception_stage
        experiment_result_path = self._save_experiment(package_name)
        self._write_agent_result(
            package_name,
            experiment_result_path,
        )

    @staticmethod
    def _trusted_terminal_outcome(
        parsed_payload: dict,
    ) -> Optional[str]:
        """Return a terminal outcome only for complete trusted evidence."""
        if not isinstance(parsed_payload, dict):
            return None

        if (
            parsed_payload.get("success") is not False
            or parsed_payload.get("status")
            != "artifact_contract_mismatch"
        ):
            return None

        evidence = parsed_payload.get(
            "outcome_evidence"
        )

        if not isinstance(evidence, dict):
            return None

        produced_count = evidence.get(
            "produced_binary_artifact_count"
        )

        valid_count = (
            isinstance(produced_count, int)
            and not isinstance(produced_count, bool)
            and produced_count > 0
        )

        package_format = evidence.get(
            "package_format"
        )
        classifier = evidence.get(
            "classifier"
        )

        trusted = (
            evidence.get("schema_version") == "0.1"
            and evidence.get("kind")
            == "artifact_contract_mismatch"
            and evidence.get(
                "underlying_build_succeeded"
            )
            is True
            and evidence.get(
                "artifact_validation_passed"
            )
            is False
            and valid_count
            and isinstance(package_format, str)
            and bool(package_format.strip())
            and evidence.get("evidence_source")
            == "validator_result"
            and isinstance(classifier, str)
            and bool(classifier.strip())
        )

        if not trusted:
            return None

        return "artifact_contract_mismatch"

    @classmethod
    def _apply_trusted_terminal_outcome(
        cls,
        parsed_payload: dict,
        loop_result: dict,
        *,
        tool_rounds: Optional[int] = None,
    ) -> bool:
        outcome = cls._trusted_terminal_outcome(
            parsed_payload
        )

        if outcome is None:
            return False

        updates = {
            "build_status": outcome,
            "stop_reason": outcome,
            "terminal_outcome": outcome,
        }

        if tool_rounds is not None:
            updates["tool_rounds"] = tool_rounds

        loop_result.update(updates)
        return True

    @staticmethod
    def _build_status_from_text(result_content: str) -> str:
        try:
            structured = json.loads(result_content)
        except json.JSONDecodeError:
            structured = None

        if isinstance(structured, dict):
            status = str(
                structured.get("status") or ""
            ).strip().lower()

            if status == "artifact_contract_mismatch":
                return status

        text = result_content.lower()
        if "timeout" in text or "timed out" in text:
            return "timeout"
        if '"success": true' in text or "'success': true" in text:
            return "success"
        if '"success": false' in text or "'success': false" in text:
            return "failed"
        return "unknown"

    @staticmethod
    def _build_execution_state(
        result_content: str,
    ) -> Tuple[bool, bool]:
        """
        Return (skipped_unchanged_input, effective_validator_run).

        A skipped call remains a valid Input Guard result, but it does
        not verify source modifications.
        """
        try:
            payload = json.loads(result_content)
        except json.JSONDecodeError:
            return False, False

        if not isinstance(payload, dict):
            return False, False

        status = str(
            payload.get("status") or ""
        ).strip().lower()

        skipped = status == "skipped_unchanged_input"

        if skipped:
            return True, False

        effective = bool(
            payload.get("duration_seconds") is not None
            or payload.get("exit_code") is not None
            or payload.get("log_path")
            or status
            in {
                "success",
                "succeeded",
                "failed",
                "timeout",
                "timed_out",
                "artifact_contract_mismatch",
            }
        )

        return False, effective

    async def connect(self, attempt: int = 1) -> bool:
        self._log("global", f"Connecting to MCP server... (attempt {attempt})")
        try:
            params = StdioServerParameters(
                command=sys.executable,
                args=[self.server_script],
                env=dict(os.environ),
            )
            stdio_transport = await self.exit_stack.enter_async_context(stdio_client(params))
            stdio, write = stdio_transport
            self.session = await self.exit_stack.enter_async_context(ClientSession(stdio, write))
            await self.session.initialize()
            self.is_session_active = True
            self._log("global", "Connected to MCP server.")
            return True
        except Exception as e:
            self._log("global", f"Connect failed: {e}")
            if attempt < self.max_retries:
                await asyncio.sleep(3)
                return await self.connect(attempt + 1)
            return False

    async def list_tools_openai_format(self) -> List[Dict]:
        assert self.session is not None
        resp = await self.session.list_tools()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.inputSchema,
                    "input_schema": t.inputSchema,
                },
            }
            for t in resp.tools
        ]
        return tools

    async def process_all_packages(self):
        if not self.is_session_active and not await self.connect():
            self._log("global", "Cannot connect to MCP server, exit.")
            return

        assert self.session is not None
        # Query server for packages to process
        pkg_resp = await self.session.call_tool(
            "get_packages_to_process", 
            {"base_dir": self.base_dir, "result_dir": self.result_dir}
        )
        pkg_info = json.loads(pkg_resp.content[0].text)
        if not pkg_info.get("success"):
            self._log("global", f"Get packages failed: {pkg_info.get('message')}")
            return

        packages = pkg_info.get("packages", [])
        self._log("global", f"Found {len(packages)} packages.")

        tools = await self.list_tools_openai_format()
        model_visible_tools = {
            "get_structure_of_files",
            "get_file_content_tool",
            "query_dependency_api_tool",
            "apply_git_unified_patch_tool",
            "prepare_debian_source_tool",
            "prepare_rpm_source_tool",
            "run_build_validation_tool",
        }
        tools = [
            t
            for t in tools
            if t.get("function", {}).get("name") in model_visible_tools
        ]
        self._log(
            "global",
            "Model-visible MCP tools: "
            + ", ".join(
                t.get("function", {}).get("name", "<unknown>")
                for t in tools
            ),
        )

        for idx, pkg in enumerate(packages, 1):
            self._log("global", f"\n=== [{idx}/{len(packages)}] {pkg} ===")
            try:
                await self.process_one_package(pkg, tools)
            except asyncio.CancelledError:
                self._finish_experiment(
                    pkg, "user_aborted", "task_cancelled", "active_task"
                )
                raise
            except KeyboardInterrupt:
                self._finish_experiment(
                    pkg, "user_aborted", "keyboard_interrupt", "active_task"
                )
                raise
            except Exception as e:
                stage = (
                    self.active_experiment or {}
                ).get("exception_stage", "unknown")
                self._finish_experiment(
                    pkg,
                    "client_error",
                    f"{type(e).__name__}: {e}",
                    stage,
                )
                self._log(pkg, f"Fatal error: {e}\n{traceback.format_exc()}")

    async def process_one_package(self, package_name: str, tools: List[Dict]):
        assert self.session is not None
        experiment = self._start_experiment(package_name)

        # Reset upload status for hard dependency enforcement
        self.upload_status[package_name] = False

        # Initialize temp env (copy package -> temp dir)
        init_ret = await self.session.call_tool(
            "init_package_environment_tool",
            {
                "base_dir": self.base_dir,
                "package_name": package_name,
                "temp_work_dir": self.temp_work_dir,
                "result_dir": self.result_dir,
            },
        )
        init_data = json.loads(init_ret.content[0].text)
        if not init_data.get("success"):
            self._log(package_name, f"Init failed: {init_data.get('message')}")
            self._finish_experiment(
                package_name,
                "initialization_failed",
                init_data.get("message", "package initialization failed"),
                "initialization",
            )
            return

        package_path = init_data["package_path"]
        result_file = init_data["result_file"]
        experiment["_result_file"] = result_file

        package_type = ""
        metadata_path = os.path.join(
            package_path,
            ".buildbench-case.json",
        )

        try:
            if os.path.isfile(metadata_path):
                with open(metadata_path, "r", encoding="utf-8") as handle:
                    workspace_metadata = json.load(handle)

                source_case_dir = workspace_metadata.get("source_case_dir")
                if isinstance(source_case_dir, str) and source_case_dir:
                    manifest_path = os.path.join(
                        source_case_dir,
                        "manifest.json",
                    )
                    if os.path.isfile(manifest_path):
                        with open(
                            manifest_path,
                            "r",
                            encoding="utf-8",
                        ) as handle:
                            manifest = json.load(handle)
                        package_type = str(
                            manifest.get("package_type", "")
                        ).strip().lower()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            package_type = ""

        if package_type not in {"deb", "rpm"}:
            try:
                workspace_entries = os.listdir(package_path)
            except OSError:
                workspace_entries = []

            if any(name.endswith(".dsc") for name in workspace_entries):
                package_type = "deb"
            elif any(name.endswith(".spec") for name in workspace_entries):
                package_type = "rpm"

        lifecycle_tools = {
            "prepare_debian_source_tool",
            "prepare_rpm_source_tool",
        }
        selected_lifecycle_tool = {
            "deb": "prepare_debian_source_tool",
            "rpm": "prepare_rpm_source_tool",
        }.get(package_type)

        tools = [
            tool
            for tool in tools
            if (
                tool.get("function", {}).get("name")
                not in lifecycle_tools
                or tool.get("function", {}).get("name")
                == selected_lifecycle_tool
            )
        ]
        self._log(
            package_name,
            "Package type: "
            + (package_type or "unknown")
            + "; source lifecycle tool: "
            + (selected_lifecycle_tool or "none"),
        )

        # Load system prompt template
        with open("prompts/patch_generation.txt", "r") as f:
            system_prompt_tpl = f.read()

        build_succeeded = False
        terminal_outcome: Optional[str] = None
        final_text = ""

        # Multiple build attempts
        for attempt in range(1, self.max_build_attempts + 1):
            attempt_result = self._start_attempt(package_name, attempt)
            experiment["exception_stage"] = f"attempt_{attempt}"
            self._log(package_name, f"--- Build attempt {attempt}/{self.max_build_attempts} ---")
            try:
                await self.session.call_tool("reset_package_cache_tool", {"package_name": package_name})
            except Exception as e:
                self._log(package_name, f"Cache clear failed on attempt {attempt}: {e}")

            upd = await self.session.call_tool(
                "update_prompt_with_history_tool",
                {
                    "package_name": package_name,
                    "package_path": package_path,
                    "build_attempt": attempt,
                    "formatted_prompt": system_prompt_tpl.format(
                        package_name=package_name, file_name=result_file, temp_dir=package_path
                    ),
                },
            )
            if getattr(upd, "isError", False):
                error_text = (
                    getattr(upd.content[0], "text", "")
                    if upd.content
                    else "MCP tool returned no content"
                )
                raise RuntimeError(
                    "update_prompt_with_history_tool failed: "
                    f"{error_text}"
                )

            if not upd.content:
                raise RuntimeError(
                    "update_prompt_with_history_tool returned no content"
                )

            raw_prompt_update = getattr(upd.content[0], "text", None)
            if not isinstance(raw_prompt_update, str):
                raise RuntimeError(
                    "update_prompt_with_history_tool returned "
                    "non-text content"
                )

            try:
                prompt_update = json.loads(raw_prompt_update)
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    "update_prompt_with_history_tool returned "
                    f"invalid JSON: {raw_prompt_update[:500]!r}"
                ) from error

            messages = prompt_update.get("messages")
            if not isinstance(messages, list):
                raise RuntimeError(
                    "update_prompt_with_history_tool response "
                    "does not contain a messages list"
                )

            if messages and messages[0].get("role") != "system":
                messages.insert(0, {"role": "system", "content": system_prompt_tpl})

            content, ok, loop_result = await self._llm_tools_loop(
                package_name, package_path, messages, tools
            )
            experiment["attempts_completed"] = attempt
            experiment["build_status"] = loop_result["build_status"]

            for event in loop_result["modification_events"]:
                merge_modification_event(experiment, event)

            experiment["stop_reason"] = loop_result["stop_reason"]
            attempt_result["tool_rounds"] = loop_result["tool_rounds"]
            attempt_result["build_status"] = loop_result["build_status"]
            attempt_result["patch_generated"] = loop_result[
                "patch_generated"
            ]
            attempt_result["patch_applied"] = loop_result["patch_applied"]
            attempt_result["unified_diff_supplied"] = loop_result[
                "unified_diff_supplied"
            ]
            attempt_result["unified_diff_applied"] = loop_result[
                "unified_diff_applied"
            ]
            attempt_result["modification_applied"] = loop_result[
                "modification_applied"
            ]
            attempt_result["modified_paths"] = list(
                loop_result["modified_paths"]
            )
            attempt_result["modification_events"] = [
                dict(event)
                for event in loop_result["modification_events"]
            ]
            trusted_terminal = (
                loop_result.get("terminal_outcome")
                == "artifact_contract_mismatch"
            )
            attempt_status = (
                "succeeded"
                if ok
                else "stopped"
                if trusted_terminal
                else "failed"
            )
            self._finish_attempt(
                package_name,
                attempt_status,
                ok,
                loop_result["stop_reason"],
            )
            self._save_experiment(package_name)
            if ok:
                build_succeeded = True
                final_text = (
                    f"Build succeeded on attempt {attempt}.\n"
                    f"Build status: {loop_result['build_status']}.\n"
                )
                break
            elif trusted_terminal:
                terminal_outcome = (
                    "artifact_contract_mismatch"
                )
                final_text = (
                    "The underlying package build succeeded, "
                    "but the original artifact contract did "
                    f"not match on attempt {attempt}.\n"
                    "Further source modification was stopped "
                    "because trusted backend evidence confirmed "
                    "an artifact-contract outcome.\n"
                    "Build status: "
                    f"{loop_result['build_status']}.\n"
                )
                break
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": f"Build failed after attempt {attempt}. Continue analyzing and repairing, then retry.",
                    }
                )
                final_text = (
                    f"Build failed on attempt {attempt}.\n"
                    f"Build status: {loop_result['build_status']}.\n"
                )

        with open(result_file, "w", encoding="utf-8") as f:
            f.write(final_text)
        self._log(package_name, f"Final saved to {result_file}")
        if build_succeeded:
            self._finish_experiment(
                package_name, "success", "build_succeeded"
            )
        elif terminal_outcome == "artifact_contract_mismatch":
            self._log(
                package_name,
                "Trusted backend evidence confirmed an "
                "artifact contract mismatch; stopping "
                "further source repair.",
            )
            self._finish_experiment(
                package_name,
                "artifact_contract_mismatch",
                "artifact_contract_mismatch",
            )
        else:
            self._log(package_name, "Max attempts reached without success.")
            final_status = (
                "build_timeout"
                if experiment["build_status"] == "timeout"
                else experiment["stop_reason"]
                if experiment["stop_reason"]
                in {
                    "max_tool_rounds",
                    "model_error",
                    "tool_error",
                    "unverified_modification",
                }
                else "build_failed"
            )
            self._finish_experiment(
                package_name,
                final_status,
                experiment["stop_reason"] or "max_build_attempts",
            )


    async def _run_modify_as_diff(self, package_name: str, file_path: str, new_content: str) -> str:
        """
        Build a diff between the old content and the new content, and apply the diff.
        
        Args:
            package_name (str): The name of the package.
            file_path (str): The path of the file to modify.
            new_content (str): The new content to apply.
        
        Returns:
            str: The result of the diff application.
        """
        assert self.session is not None
        # 1) generate unified diff
        diff_res = await self.session.call_tool(
            "propose_unified_diff_tool",
            {"file_path": file_path, "new_content": new_content},
        )
        diff_text = diff_res.content[0].text
        # 2) apply unified diff
        apply_res = await self.session.call_tool(
            "apply_unified_diff_tool",
            {"file_path": file_path, "diff_text": diff_text},
        )
        apply_text = apply_res.content[0].text
        head = "\n".join(diff_text.splitlines()[:30])
        return (
            "[modify_file_tool shimmed to unified-diff]\n"
            f"--- DIFF PREVIEW ---\n"
            f"{head}...\n"
            f"--- APPLY RESULT ---\n"
            f"{apply_text}"
        )

    async def _llm_tools_loop(
        self, package_name: str, package_path: str, messages: List[Dict], tools: List[Dict]
    ) -> Tuple[str, bool, Dict[str, Any]]:
        choice = None
        latest_text = ""
        rounds = 0
        did_build = False
        validation_required = False
        loop_result = {
            "tool_rounds": 0,
            "build_status": "not_run",
            "patch_generated": False,
            "patch_applied": False,
            "unified_diff_supplied": False,
            "unified_diff_applied": False,
            "modification_applied": False,
            "modified_paths": [],
            "modification_events": [],
            "stop_reason": None,
            "terminal_outcome": None,
        }

        # Initial model step
        try:
            self._add_attempt_metric(package_name, "model_call_count")
            resp = self.client.chat.completions.create(
                model=self.llm_cfg.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            self._record_token_usage(package_name, resp)
            choice = resp.choices[0]
            latest_text = choice.message.content or ""
        except Exception as e:
            self._log(package_name, f"Model call failed: {e}")
            loop_result["stop_reason"] = "model_error"
            return f"Model call failed: {e}", False, loop_result

        while rounds < self.max_tool_rounds and choice.finish_reason in ("tool_calls", None):
            rounds += 1
            if (
                self.active_experiment
                and self.active_experiment.get("case_id") == package_name
            ):
                self.active_experiment["tool_rounds"] += 1
            self._add_attempt_metric(package_name, "tool_rounds")
            self._log(package_name, f"== Tool round {rounds} ==")

            for tc in choice.message.tool_calls or []:
                self._add_attempt_metric(package_name, "tool_call_count")
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_args = {}
                self._log(package_name, f"Tool call: {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:500]})")

                # Repeat guard
                args_key = make_args_key(tool_name, tool_args)
                repeat_check = await self.session.call_tool(
                    "check_repeat_tool_call",
                    {
                        "tool_name": tool_name,
                        "args_key": args_key,
                        "max_repeat": 3,
                        "package_name": package_name,
                    },
                )
                repeat_allowed = json.loads(repeat_check.content[0].text).get("allowed", True)

                if tool_name == "check_build_result" and not self.upload_status.get(package_name, False):
                    tool_ret = (
                        "ERROR: Cannot call check_build_result before uploading. "
                        "You must call upload_file_to_obs_tool first."
                    )
                    # Feed back the error as tool result
                    messages.append(
                        {
                            "role": "assistant",
                            "content": choice.message.content,
                            "tool_calls": [t.model_dump() for t in (choice.message.tool_calls or [])],
                        }
                    )
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": tool_ret})
                    continue

                if not repeat_allowed:
                    # Block and nudge
                    tool_ret = json.loads(repeat_check.content[0].text).get(
                        "message", "repeated call blocked"
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Do not call {tool_name} again in this attempt. Continue with code changes or other tools.",
                        }
                    )
                else:
                    # Cache lookup
                    cache = await self.session.call_tool(
                        "check_tool_cache",
                        {"call_key": args_key, "tool_name": tool_name, "package_name": package_name},
                    )
                    cache_data = json.loads(cache.content[0].text)
                    if cache_data.get("hit"):
                        tool_ret = cache_data["result"]
                    else:
                        try:
                            timeout = (
                                self.build_tool_timeout_seconds
                                if tool_name == "run_build_validation_tool"
                                else 600
                            )
                            self._add_attempt_metric(
                                package_name, "tool_execution_count"
                            )
                            if tool_name == "run_build_validation_tool":
                                self._add_attempt_metric(
                                    package_name, "build_call_count"
                                )
                            res = await asyncio.wait_for(
                                self.session.call_tool(tool_name, tool_args), timeout=timeout
                            )
                            tool_ret = res.content[0].text
                            self._log(package_name, f"Tool return text: {tool_ret[:1000]}")
                            # Cache only for safe/beneficial tools (example from original)
                            if tool_name in ["propose_unified_diff_tool", "apply_unified_diff_tool"] and "error" not in tool_ret.lower():
                                await self.session.call_tool(
                                    "cache_tool_result",
                                    {"call_key": args_key, "result": tool_ret, "package_name": package_name},
                                )
                        except asyncio.TimeoutError:
                            tool_ret = f"Error: Tool {tool_name} timed out"
                            loop_result["stop_reason"] = (
                                "build_timeout"
                                if tool_name == "run_build_validation_tool"
                                else "tool_error"
                            )
                        except Exception as e:
                            tool_ret = f"Error: Tool {tool_name} failed: {e}"
                            loop_result["stop_reason"] = "tool_error"

                    # Record history of tool calls regardless of cache
                    await self.session.call_tool(
                        "record_tool_call_history", {"call_key": args_key, "package_name": package_name}
                    )

                if tool_name in ["upload_file_to_obs_tool"]:
                    if "successful" in tool_ret.lower() or "success" in tool_ret.lower():
                        self.upload_status[package_name] = True
                        did_build = True
                        self._log(package_name, "✓ Upload marked as successful")

                if tool_name == "run_build_validation_tool":
                    did_build = True
                    loop_result["build_status"] = (
                        self._build_status_from_text(tool_ret)
                    )

                    _, effective_build = (
                        self._build_execution_state(tool_ret)
                    )

                    if effective_build:
                        validation_required = False

                    if (
                        self.active_experiment
                        and self.active_experiment.get("case_id")
                        == package_name
                    ):
                        self.active_experiment["build_status"] = (
                            loop_result["build_status"]
                        )

                if tool_name == "apply_git_unified_patch_tool":
                    modification_event = parse_modification_tool_result(
                        tool_args,
                        tool_ret,
                    )
                    modification_event["tool_round"] = rounds
                    merge_modification_event(
                        loop_result,
                        modification_event,
                    )

                    if modification_event.get("success"):
                        validation_required = True

                # Feed back results to the model
                messages.append(
                    {"role": "assistant", "content": choice.message.content, "tool_calls": [t.model_dump() for t in (choice.message.tool_calls or [])]}
                )
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": tool_ret})

                # If it's a build verification, parse immediately
                if tool_name == "check_build_result":
                    parsed = await self.session.call_tool(
                        "parse_build_result_tool", {"result_content": tool_ret, "package_name": package_name}
                    )
                    if json.loads(parsed.content[0].text).get("success"):
                        loop_result.update(
                            tool_rounds=rounds,
                            build_status="success",
                            stop_reason="build_succeeded",
                        )
                        return latest_text, True, loop_result
                elif tool_name == "run_build_validation_tool":
                    parsed = await self.session.call_tool(
                        "parse_build_result_tool",
                        {"result_content": tool_ret, "package_name": package_name},
                    )
                    parsed_payload = json.loads(
                        parsed.content[0].text
                    )
                    if parsed_payload.get("success"):
                        loop_result.update(
                            tool_rounds=rounds,
                            build_status="success",
                            stop_reason="build_succeeded",
                        )
                        return latest_text, True, loop_result
                    if self._apply_trusted_terminal_outcome(
                        parsed_payload,
                        loop_result,
                        tool_rounds=rounds,
                    ):
                        return latest_text, False, loop_result

            # next model step
            try:
                self._add_attempt_metric(package_name, "model_call_count")
                resp = self.client.chat.completions.create(
                    model=self.llm_cfg.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                )
                self._record_token_usage(package_name, resp)
                choice = resp.choices[0]
                latest_text = choice.message.content or latest_text
            except Exception as e:
                self._log(package_name, f"Model continuation failed: {e}")
                loop_result["stop_reason"] = "model_error"
                break

        loop_result["tool_rounds"] = rounds
        if (
            rounds >= self.max_tool_rounds
            and choice is not None
            and choice.finish_reason in ("tool_calls", None)
        ):
            loop_result["stop_reason"] = "max_tool_rounds"

        # Run a final validation when no build was requested, or when
        # successful source modifications occurred after the latest effective
        # Validator run. The Input Guard remains enabled.
        if not did_build or validation_required:
            try:
                self._add_attempt_metric(
                    package_name, "build_call_count"
                )
                self._add_attempt_metric(
                    package_name, "fallback_build_count"
                )
                build_res = await asyncio.wait_for(
                    self.session.call_tool(
                        "run_build_validation_tool", {"package_path": package_path}
                    ),
                    timeout=self.build_tool_timeout_seconds,
                )
                build_txt = build_res.content[0].text
                self._log(package_name, f"[fallback] run_build_validation_tool => {build_txt[:500]}")
                did_build = True
                loop_result["build_status"] = (
                    self._build_status_from_text(build_txt)
                )

                _, effective_build = (
                    self._build_execution_state(build_txt)
                )

                if effective_build:
                    validation_required = False

                if (
                    self.active_experiment
                    and self.active_experiment.get("case_id") == package_name
                ):
                    self.active_experiment["build_status"] = (
                        loop_result["build_status"]
                    )
                parsed = await self.session.call_tool(
                    "parse_build_result_tool",
                    {"result_content": build_txt, "package_name": package_name},
                )
                parsed_payload = json.loads(
                    parsed.content[0].text
                )
                if parsed_payload.get("success"):
                    loop_result.update(
                        build_status="success",
                        stop_reason="build_succeeded",
                    )
                    return latest_text, True, loop_result
                if self._apply_trusted_terminal_outcome(
                    parsed_payload,
                    loop_result,
                ):
                    return latest_text, False, loop_result
            except asyncio.TimeoutError:
                loop_result["build_status"] = "timeout"
                loop_result["stop_reason"] = "build_timeout"
                self._log(package_name, "[fallback] build validation timed out")
            except Exception as e:
                loop_result["stop_reason"] = "tool_error"
                self._log(package_name, f"[fallback] build validation failed: {e}")

        if (
            validation_required
            and loop_result["stop_reason"]
            in {
                None,
                "max_tool_rounds",
                "build_failed",
            }
        ):
            loop_result["stop_reason"] = "unverified_modification"

        if did_build and loop_result["build_status"] == "not_run":
            loop_result["build_status"] = "unknown"
        if not loop_result["stop_reason"]:
            loop_result["stop_reason"] = (
                "build_failed"
                if did_build
                else "model_completed_without_build"
            )
        return latest_text, False, loop_result

    async def cleanup(self):
        try:
            await self.exit_stack.aclose()
        except Exception as e:
            self._log("global", f"Cleanup error: {e}")
        self.is_session_active = False
        self.session = None
        self._log("global", "Cleanup completed.")


async def main():
    # Resolve once so Client and inherited MCP Server use one config.
    config_path = resolve_agent_config_path()
    config = load_agent_config(config_path)
    os.environ["BB_AGENT_CONFIG"] = str(config_path)

    # Choose provider + model here.
    provider = config["LLM_PROVIDER"].lower()
    default_model = {
        "openai": "gpt-5",
        "qwen": os.getenv("LLM_MODEL", "qwen3-max"),
        "claude": "claude-sonnet-4-5-20250929",
        "deepseek": "deepseek-v3",
    }.get(provider)

    llm_cfg = LLMConfig(
        provider=provider,
        model=default_model,
    )
    cli = AutoRepairClientPatch(
        llm=llm_cfg,
        config=config,
        config_path=str(config_path),
    )
    try:
        await cli.process_all_packages()
    finally:
        await cli.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
