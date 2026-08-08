import argparse
import asyncio
import os
import json
import time
import traceback
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from contextlib import AsyncExitStack
import yaml
from dotenv import load_dotenv
from openai import OpenAI
from mcp.client.stdio import stdio_client
from mcp import ClientSession, StdioServerParameters

load_dotenv(".env")
with open("config/info.yaml", "r") as f:
    info = yaml.safe_load(f)


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
            raise RuntimeError(
                f"Missing API key for provider={self.provider}. Check your environment.")

    def make_client(self) -> OpenAI:
        if self.base_url:
            return OpenAI(api_key=self.api_key, base_url=self.base_url)
        return OpenAI(api_key=self.api_key)


def make_args_key(tool_name: str, tool_args: dict) -> str:
    return f"{tool_name}::{json.dumps(tool_args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))}"


class AutoRepairClient:
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
        server_script: str = "server.py",
        max_retries: int = 2,
        max_build_attempts: int = 3,
        max_tool_rounds: int = 20,
        result_dir: Optional[str] = None,
        log_dir: Optional[str] = None,
        temp_work_dir: Optional[str] = None,
    ) -> None:
        self.exit_stack = AsyncExitStack()
        self.session: Optional[ClientSession] = None
        self.is_session_active = False

        # LLM adapter
        self.llm_cfg = llm
        self.client = self.llm_cfg.make_client()

        # Paths (choose sensible default if not given)
        
        self.base_dir = base_dir or info["paths"]["base_dir"]
        self.result_dir = result_dir or info["paths"]["result_dir"]
        self.log_dir = log_dir or info["paths"]["log_dir"]
        self.temp_work_dir = temp_work_dir or info["paths"]["temp_work_dir"]

        os.makedirs(self.result_dir, exist_ok=True)
        os.makedirs(self.temp_work_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)

        self.server_script = server_script
        self.max_retries = max_retries
        self.max_build_attempts = max_build_attempts
        self.max_tool_rounds = max_tool_rounds
        self.build_tool_timeout_seconds = int(
            (info.get("build") or {}).get("client_tool_timeout_seconds", 7200)
        )

        # Per-package state
        self.upload_status: Dict[str, bool] = {}

    def _log(self, tag: str, msg: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        path = os.path.join(self.log_dir, f"{tag}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
        print(msg)

    async def connect(self, attempt: int = 1) -> bool:
        self._log("global", f"Connecting to MCP server... (attempt {attempt})")
        try:
            params = StdioServerParameters(
                command=sys.executable,
                args=[self.server_script],
                env=dict(os.environ),
            )
            stdio_transport = await self.exit_stack.enter_async_context(
                stdio_client(params)
            )
            stdio, write = stdio_transport
            self.session = await self.exit_stack.enter_async_context(
                ClientSession(stdio, write)
            )
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

    async def list_agent_tools_openai_format(self) -> List[Dict]:
        """Return the tool contract exposed to an Agent."""
        tools = await self.list_tools_openai_format()
        hidden_tools = {
            "init_package_environment_tool",
            "upload_file_to_obs_tool",
            "upload_file_to_obs_tool_deepseek",
            "check_build_result",
        }
        return [
            tool
            for tool in tools
            if tool.get("function", {}).get("name") not in hidden_tools
        ]

    def discover_packages(self) -> List[str]:
        """Discover package directories without starting a coordinator MCP server."""
        base_dir = Path(self.base_dir)
        if not base_dir.is_dir():
            raise RuntimeError(f"Base directory not found: {base_dir}")
        return sorted(path.name for path in base_dir.iterdir() if path.is_dir())

    def create_package_worker(self) -> "AutoRepairClient":
        """Create an isolated Agent/MCP lifecycle for one concurrent Case."""
        return AutoRepairClient(
            llm=self.llm_cfg,
            base_dir=self.base_dir,
            server_script=self.server_script,
            max_retries=self.max_retries,
            max_build_attempts=self.max_build_attempts,
            max_tool_rounds=self.max_tool_rounds,
            result_dir=self.result_dir,
            log_dir=self.log_dir,
            temp_work_dir=self.temp_work_dir,
        )

    async def process_package_isolated(
        self, package_name: str, index: int, total: int
    ) -> None:
        """Process one Case with a dedicated client, LLM client and MCP session."""
        worker: Optional[AutoRepairClient] = None
        self._log("global", f"\n=== [{index}/{total}] {package_name} ===")
        try:
            worker = self.create_package_worker()
            if not await worker.connect():
                worker._log(package_name, "Cannot connect to MCP server, skipped.")
                return
            tools = await worker.list_agent_tools_openai_format()
            await worker.process_one_package(package_name, tools)
        except Exception as error:
            logger = worker._log if worker is not None else self._log
            logger(
                package_name,
                f"Fatal error: {error}\n{traceback.format_exc()}",
            )
        finally:
            if worker is not None:
                await worker.cleanup()

    async def process_packages_concurrently(
        self, packages: List[str], concurrency: int
    ) -> None:
        """Run different Cases concurrently while keeping each Case sequential."""
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        semaphore = asyncio.Semaphore(concurrency)

        async def run_one(index: int, package_name: str) -> None:
            async with semaphore:
                await self.process_package_isolated(package_name, index, len(packages))

        await asyncio.gather(
            *(run_one(index, package) for index, package in enumerate(packages, 1))
        )

    async def process_all_packages(self, concurrency: int = 1):
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")

        try:
            packages = self.discover_packages()
        except RuntimeError as error:
            self._log("global", str(error))
            return

        self._log("global", f"Found {len(packages)} packages.")
        if concurrency > 1:
            self._log(
                "global",
                f"Processing packages with Case concurrency={concurrency}.",
            )
            await self.process_packages_concurrently(packages, concurrency)
            return

        if not self.is_session_active and not await self.connect():
            self._log("global", "Cannot connect to MCP server, exit.")
            return

        tools = await self.list_agent_tools_openai_format()

        for idx, pkg in enumerate(packages, 1):
            self._log("global", f"\n=== [{idx}/{len(packages)}] {pkg} ===")
            try:
                await self.process_one_package(pkg, tools)
            except Exception as e:
                self._log(pkg, f"Fatal error: {e}\n{traceback.format_exc()}")

    async def process_one_package(self, package_name: str, tools: List[Dict]):
        assert self.session is not None

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
            return

        package_path = init_data["package_path"]
        result_file = init_data["result_file"]

        # Load system prompt template
        with open("prompts/full_file_generation.txt", "r", encoding="utf-8") as f:
            system_prompt_tpl = f.read()

        build_succeeded = False
        final_text = ""

        # Multiple build attempts
        for attempt in range(1, self.max_build_attempts + 1):
            self._log(
                package_name,
                f"--- Build attempt {attempt}/{self.max_build_attempts} ---",
            )
            try:
                await self.session.call_tool(
                    "reset_package_cache_tool", {"package_name": package_name}
                )
            except Exception as e:
                self._log(package_name, f"Cache clear failed on attempt {attempt}: {e}")

            upd = await self.session.call_tool(
                "update_prompt_with_history_tool",
                {
                    "package_name": package_name,
                    "package_path": package_path,
                    "build_attempt": attempt,
                    "formatted_prompt": system_prompt_tpl.format(
                        package_name=package_name,
                        file_name=result_file,
                        temp_dir=package_path,
                    ),
                },
            )
            messages = json.loads(upd.content[0].text)["messages"]

            # Ensure the first message is a system message for OpenAI-chat format
            if messages and messages[0].get("role") != "system":
                messages.insert(0, {"role": "system", "content": system_prompt_tpl})

            content, ok = await self._llm_tools_loop(
                package_name, package_path, messages, tools
            )
            if ok:
                build_succeeded = True
                final_text = f"Build succeeded on attempt {attempt}.\n{content or ''}"
                break
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": f"Build failed after attempt {attempt}. Continue analyzing and repairing, then retry.",
                    }
                )
                final_text = f"Build failed on attempt {attempt}.\n{content or ''}"

        with open(result_file, "w", encoding="utf-8") as f:
            f.write(final_text)
        self._log(package_name, f"Final saved to {result_file}")
        if not build_succeeded:
            self._log(package_name, "Max attempts reached without success.")

    async def _llm_tools_loop(
        self,
        package_name: str,
        package_path: str,
        messages: List[Dict],
        tools: List[Dict],
    ) -> Tuple[str, bool]:
        choice = None
        latest_text = ""
        rounds = 0
        did_build = False

        # Initial model step
        try:
            resp = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=self.llm_cfg.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            choice = resp.choices[0]
            latest_text = choice.message.content or ""
        except Exception as e:
            self._log(package_name, f"Model call failed: {e}")
            return f"Model call failed: {e}", False

        while rounds < self.max_tool_rounds and choice.finish_reason in (
            "tool_calls",
            None,
        ):
            rounds += 1
            self._log(package_name, f"== Tool round {rounds} ==")

            for tc in choice.message.tool_calls or []:
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_args = {}
                self._log(
                    package_name,
                    f"Tool call: {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:500]})",
                )

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
                repeat_allowed = json.loads(repeat_check.content[0].text).get(
                    "allowed", True
                )

                # Enforce upload-before-check rule (from client_claude)
                if tool_name == "check_build_result" and not self.upload_status.get(
                    package_name, False
                ):
                    tool_ret = (
                        "ERROR: Cannot call check_build_result before uploading. "
                        "You must call upload_file_to_obs_tool first."
                    )
                    # Feed back the error as tool result
                    messages.append(
                        {
                            "role": "assistant",
                            "content": choice.message.content,
                            "tool_calls": [
                                t.model_dump()
                                for t in (choice.message.tool_calls or [])
                            ],
                        }
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": tool_ret}
                    )
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
                        {
                            "call_key": args_key,
                            "tool_name": tool_name,
                            "package_name": package_name,
                        },
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
                            res = await asyncio.wait_for(
                                self.session.call_tool(tool_name, tool_args),
                                timeout=timeout,
                            )
                            tool_ret = res.content[0].text
                            self._log(
                                package_name, f"Tool return text: {tool_ret[:1000]}"
                            )
                            # Cache only for safe/beneficial tools (example from original)
                            if (
                                tool_name in ["modify_file_tool"]
                                and "error" not in tool_ret.lower()
                            ):
                                await self.session.call_tool(
                                    "cache_tool_result",
                                    {
                                        "call_key": args_key,
                                        "result": tool_ret,
                                        "package_name": package_name,
                                    },
                                )
                        except asyncio.TimeoutError:
                            tool_ret = f"Error: Tool {tool_name} timed out"
                        except Exception as e:
                            tool_ret = f"Error: Tool {tool_name} failed: {e}"

                    # Record history of tool calls regardless of cache
                    await self.session.call_tool(
                        "record_tool_call_history",
                        {"call_key": args_key, "package_name": package_name},
                    )

                if tool_name in [
                    "upload_file_to_obs_tool",
                    "upload_file_to_obs_tool_deepseek",
                ]:
                    if (
                        "successful" in tool_ret.lower()
                        or "success" in tool_ret.lower()
                    ):
                        self.upload_status[package_name] = True
                        did_build = True
                        self._log(package_name, "✓ Upload marked as successful")

                if tool_name == "run_build_validation_tool":
                    did_build = True

                # Feed back results to the model
                messages.append(
                    {
                        "role": "assistant",
                        "content": choice.message.content,
                        "tool_calls": [
                            t.model_dump() for t in (choice.message.tool_calls or [])
                        ],
                    }
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": tool_ret}
                )

                # If it's a build verification, parse immediately
                if tool_name == "check_build_result":
                    parsed = await self.session.call_tool(
                        "parse_build_result_tool",
                        {"result_content": tool_ret, "package_name": package_name},
                    )
                    if json.loads(parsed.content[0].text).get("success"):
                        return latest_text, True
                elif tool_name == "run_build_validation_tool":
                    parsed = await self.session.call_tool(
                        "parse_build_result_tool",
                        {"result_content": tool_ret, "package_name": package_name},
                    )
                    if json.loads(parsed.content[0].text).get("success"):
                        return latest_text, True

            # next model step
            try:
                resp = await asyncio.to_thread(
                    self.client.chat.completions.create,
                    model=self.llm_cfg.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                )
                choice = resp.choices[0]
                latest_text = choice.message.content or latest_text
            except Exception as e:
                self._log(package_name, f"Model continuation failed: {e}")
                break

        # Fallback: always perform one real build if the model forgot to validate.
        if not did_build:
            try:
                build_res = await asyncio.wait_for(
                    self.session.call_tool(
                        "run_build_validation_tool", {"package_path": package_path}
                    ),
                    timeout=self.build_tool_timeout_seconds,
                )
                build_txt = build_res.content[0].text
                self._log(
                    package_name,
                    f"[fallback] run_build_validation_tool => {build_txt[:500]}",
                )
                parsed = await self.session.call_tool(
                    "parse_build_result_tool",
                    {"result_content": build_txt, "package_name": package_name},
                )
                if json.loads(parsed.content[0].text).get("success"):
                    return latest_text, True
            except Exception as e:
                self._log(package_name, f"[fallback] build validation failed: {e}")

        return latest_text, False

    async def cleanup(self):
        try:
            await self.exit_stack.aclose()
        except Exception as e:
            self._log("global", f"Cleanup error: {e}")
        self.is_session_active = False
        self.session = None
        self._log("global", "Cleanup completed.")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Build-Bench iterative repair Agent."
    )
    parser.add_argument(
        "--concurrency",
        type=positive_int,
        default=positive_int(os.getenv("BUILD_BENCH_CONCURRENCY", "1")),
        help="maximum number of Cases processed concurrently (default: 1)",
    )
    parser.add_argument(
        "--base-dir",
        help="directory whose immediate subdirectories are standard Cases",
    )
    parser.add_argument(
        "--run-dir",
        help="isolate Agent logs, workspaces and Docker build results under this directory",
    )
    parser.add_argument(
        "--max-build-attempts",
        type=positive_int,
        default=3,
    )
    parser.add_argument(
        "--max-tool-rounds",
        type=positive_int,
        default=20,
    )
    return parser.parse_args(argv)


async def main(argv: Optional[List[str]] = None):
    args = parse_args(argv)
    # Choose provider + model here.
    provider = info["LLM_PROVIDER"].lower()
    default_model = {
        "openai": "gpt-5",
        "qwen": os.getenv("LLM_MODEL", "qwen3-max"),
        "claude": "claude-sonnet-4-5-20250929",
        "deepseek": "deepseek-v3",
    }.get(provider)

    llm_cfg = LLMConfig(provider=provider, model=default_model)
    result_dir = None
    log_dir = None
    temp_work_dir = None
    if args.run_dir:
        run_dir = Path(args.run_dir).expanduser().resolve()
        result_dir = str(run_dir / "result_text_res")
        log_dir = str(run_dir / "result_log_res")
        temp_work_dir = str(run_dir / "temp_workspace")
        os.environ["BUILD_BENCH_RESULT_ROOT"] = str(run_dir / "docker-builds")
        os.environ["BUILD_BENCH_STAGING_ROOT"] = str(run_dir / "staging")

    cli = AutoRepairClient(
        llm=llm_cfg,
        base_dir=args.base_dir,
        max_build_attempts=args.max_build_attempts,
        max_tool_rounds=args.max_tool_rounds,
        result_dir=result_dir,
        log_dir=log_dir,
        temp_work_dir=temp_work_dir,
    )
    try:
        await cli.process_all_packages(concurrency=args.concurrency)
    finally:
        await cli.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
