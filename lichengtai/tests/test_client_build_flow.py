from __future__ import annotations

import json
import types
import unittest

from client import AutoRepairClient
from client_patch import (
    AutoRepairClientPatch,
    merge_modification_event,
    parse_modification_tool_result,
)


class _ToolResult:
    def __init__(self, text: str) -> None:
        self.content = [types.SimpleNamespace(text=text)]


class _ToolCall:
    def __init__(self, name: str, arguments: dict[str, object]) -> None:
        self.id = "call-1"
        self.function = types.SimpleNamespace(
            name=name, arguments=json.dumps(arguments)
        )

    def model_dump(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


class _Completions:
    def __init__(self, with_tool_call: bool) -> None:
        self.with_tool_call = with_tool_call

    def create(self, **_: object) -> object:
        tool_calls = (
            [_ToolCall("run_build_validation_tool", {"package_path": "/tmp/pkg"})]
            if self.with_tool_call
            else []
        )
        choice = types.SimpleNamespace(
            finish_reason="tool_calls" if tool_calls else "stop",
            message=types.SimpleNamespace(content="repair", tool_calls=tool_calls),
        )
        return types.SimpleNamespace(choices=[choice])


class _Session:
    def __init__(self) -> None:
        self.build_calls = 0

    async def call_tool(self, name: str, arguments: dict[str, object]) -> _ToolResult:
        if name == "check_repeat_tool_call":
            return _ToolResult('{"allowed": true}')
        if name == "check_tool_cache":
            return _ToolResult('{"hit": false}')
        if name == "record_tool_call_history":
            return _ToolResult("ok")
        if name == "run_build_validation_tool":
            self.build_calls += 1
            return _ToolResult('{"success": true, "status": "succeeded"}')
        if name == "parse_build_result_tool":
            payload = json.loads(str(arguments["result_content"]))
            return _ToolResult(
                json.dumps(
                    {
                        "success": payload["success"],
                        "status": payload["status"],
                    }
                )
            )
        raise AssertionError(f"unexpected tool: {name}")



class _ScriptedCompletions:
    def __init__(self) -> None:
        self.steps = [
            (
                "run_build_validation_tool",
                {"package_path": "/tmp/pkg"},
            ),
            (
                "apply_git_unified_patch_tool",
                {
                    "repo_root": "/tmp/pkg/debian_source",
                    "patch_text": (
                        "diff --git a/src/main.py "
                        "b/src/main.py"
                    ),
                },
            ),
            ("stop", {}),
        ]

    def create(self, **_: object) -> object:
        if not self.steps:
            raise AssertionError("scripted completion exhausted")

        name, arguments = self.steps.pop(0)

        if name == "stop":
            tool_calls = []
            finish_reason = "stop"
        else:
            tool_calls = [_ToolCall(name, arguments)]
            finish_reason = "tool_calls"

        choice = types.SimpleNamespace(
            finish_reason=finish_reason,
            message=types.SimpleNamespace(
                content="repair",
                tool_calls=tool_calls,
            ),
        )

        return types.SimpleNamespace(
            choices=[choice],
            usage=None,
        )


class _SkippedThenModifiedSession:
    def __init__(self) -> None:
        self.build_calls = 0

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> _ToolResult:
        if name == "check_repeat_tool_call":
            return _ToolResult('{"allowed": true}')

        if name == "check_tool_cache":
            return _ToolResult('{"hit": false}')

        if name == "record_tool_call_history":
            return _ToolResult("ok")

        if name == "apply_git_unified_patch_tool":
            return _ToolResult(
                "Success: applied patch\n"
                "M src/main.py"
            )

        if name == "run_build_validation_tool":
            self.build_calls += 1

            if self.build_calls == 1:
                return _ToolResult(
                    json.dumps(
                        {
                            "success": False,
                            "status": "skipped_unchanged_input",
                            "duration_seconds": None,
                            "exit_code": None,
                            "log_path": None,
                        }
                    )
                )

            return _ToolResult(
                json.dumps(
                    {
                        "success": False,
                        "status": "failed",
                        "duration_seconds": 1,
                        "exit_code": 1,
                        "log_path": "/tmp/build.log",
                    }
                )
            )

        if name == "parse_build_result_tool":
            payload = json.loads(
                str(arguments["result_content"])
            )
            return _ToolResult(
                json.dumps(
                    {
                        "success": bool(
                            payload.get("success", False)
                        ),
                        "status": payload.get(
                            "status",
                            "unknown",
                        ),
                    }
                )
            )

        raise AssertionError(f"unexpected tool: {name}")


def _make_client(client_type: type, *, with_tool_call: bool) -> object:
    instance = object.__new__(client_type)
    instance.session = _Session()
    instance.client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=_Completions(with_tool_call))
    )
    instance.llm_cfg = types.SimpleNamespace(model="fake")
    instance.max_tool_rounds = 2
    instance.build_tool_timeout_seconds = 10
    instance.upload_status = {}
    instance.active_experiment = None
    instance._log = lambda *_: None
    return instance


class ModificationResultTests(unittest.TestCase):
    def test_exact_replace_is_a_modification_not_a_unified_diff(self) -> None:
        event = parse_modification_tool_result(
            {
                "mode": "exact_replace",
                "file_path": "debian/patches/fix.patch",
                "old_text": "old",
                "new_text": "new",
            },
            (
                "Success: exact_replace\n"
                "M debian/patches/fix.patch\n"
                "replacements=1"
            ),
        )

        self.assertTrue(event["success"])
        self.assertEqual(event["operation"], "exact_replace")
        self.assertEqual(
            event["modified_paths"],
            ["debian/patches/fix.patch"],
        )
        self.assertEqual(event["replacement_count"], 1)
        self.assertFalse(event["unified_diff_supplied"])
        self.assertFalse(event["unified_diff_applied"])

    def test_unified_diff_is_recorded_as_supplied_and_applied(self) -> None:
        event = parse_modification_tool_result(
            {
                "mode": "unified_diff",
                "patch_text": (
                    "diff --git a/Makefile b/Makefile\n"
                    "--- a/Makefile\n"
                    "+++ b/Makefile\n"
                ),
            },
            "Success: applied patch\nM Makefile",
        )

        self.assertTrue(event["success"])
        self.assertEqual(event["operation"], "unified_diff")
        self.assertEqual(event["modified_paths"], ["Makefile"])
        self.assertTrue(event["unified_diff_supplied"])
        self.assertTrue(event["unified_diff_applied"])

    def test_future_json_result_is_supported(self) -> None:
        event = parse_modification_tool_result(
            {
                "mode": "exact_replace",
                "file_path": "src/main.c",
            },
            json.dumps(
                {
                    "success": True,
                    "operation": "exact_replace",
                    "modified_paths": ["src/main.c"],
                    "replacement_count": 2,
                }
            ),
        )

        self.assertTrue(event["success"])
        self.assertEqual(event["modified_paths"], ["src/main.c"])
        self.assertEqual(event["replacement_count"], 2)

    def test_merge_accumulates_multiple_modifications(self) -> None:
        target: dict[str, object] = {}

        exact_event = parse_modification_tool_result(
            {
                "mode": "exact_replace",
                "file_path": "debian/rules",
            },
            "Success: exact_replace\nM debian/rules\nreplacements=1",
        )
        diff_event = parse_modification_tool_result(
            {
                "mode": "unified_diff",
                "patch_text": "diff --git a/Makefile b/Makefile",
            },
            "Success: applied patch\nM Makefile",
        )

        merge_modification_event(target, exact_event)
        merge_modification_event(target, diff_event)

        self.assertTrue(target["modification_applied"])
        self.assertTrue(target["patch_generated"])
        self.assertTrue(target["patch_applied"])
        self.assertEqual(
            target["modified_paths"],
            ["debian/rules", "Makefile"],
        )
        self.assertEqual(len(target["modification_events"]), 2)


class ClientBuildFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_build_tool_ends_attempt_on_success(self) -> None:
        for client_type in (AutoRepairClient, AutoRepairClientPatch):
            with self.subTest(client=client_type.__name__):
                client = _make_client(client_type, with_tool_call=True)
                result = await client._llm_tools_loop(
                    "pkg", "/tmp/pkg", [{"role": "user", "content": "repair"}], []
                )
                succeeded = result[1]
                self.assertTrue(succeeded)
                self.assertEqual(client.session.build_calls, 1)

    async def test_fallback_builds_when_model_forgets_validation(self) -> None:
        for client_type in (AutoRepairClient, AutoRepairClientPatch):
            with self.subTest(client=client_type.__name__):
                client = _make_client(client_type, with_tool_call=False)
                result = await client._llm_tools_loop(
                    "pkg", "/tmp/pkg", [{"role": "user", "content": "repair"}], []
                )
                succeeded = result[1]
                self.assertTrue(succeeded)
                self.assertEqual(client.session.build_calls, 1)


    async def test_modification_after_skipped_build_gets_final_validation(
        self,
    ) -> None:
        client = object.__new__(AutoRepairClientPatch)
        session = _SkippedThenModifiedSession()

        client.session = session
        client.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=_ScriptedCompletions()
            )
        )
        client.llm_cfg = types.SimpleNamespace(model="fake")
        client.max_tool_rounds = 2
        client.build_tool_timeout_seconds = 10
        client.upload_status = {}
        client.active_experiment = None
        client._log = lambda *_: None

        _, succeeded, loop_result = await client._llm_tools_loop(
            "pkg",
            "/tmp/pkg",
            [{"role": "user", "content": "repair"}],
            [],
        )

        self.assertFalse(succeeded)
        self.assertEqual(session.build_calls, 2)
        self.assertTrue(
            loop_result["modification_applied"]
        )
        self.assertEqual(
            loop_result["build_status"],
            "failed",
        )
        self.assertEqual(
            loop_result["stop_reason"],
            "build_failed",
        )


if __name__ == "__main__":
    unittest.main()
