from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from client import AutoRepairClient, parse_args


class _Tracker:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.started: list[str] = []
        self.finished: list[str] = []
        self.worker_ids: list[int] = []
        self.cleaned_worker_ids: list[int] = []


class _FakeWorker:
    def __init__(self, worker_id: int, tracker: _Tracker) -> None:
        self.worker_id = worker_id
        self.tracker = tracker

    def _log(self, *_: object) -> None:
        return None

    async def connect(self) -> bool:
        return True

    async def list_agent_tools_openai_format(self) -> list[dict]:
        return []

    async def process_one_package(
        self, package_name: str, _tools: list[dict]
    ) -> None:
        self.tracker.worker_ids.append(self.worker_id)
        self.tracker.started.append(package_name)
        self.tracker.active += 1
        self.tracker.max_active = max(
            self.tracker.max_active, self.tracker.active
        )
        await asyncio.sleep(0.04)
        self.tracker.active -= 1
        self.tracker.finished.append(package_name)

    async def cleanup(self) -> None:
        self.tracker.cleaned_worker_ids.append(self.worker_id)


class ClientConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def _coordinator(self, tracker: _Tracker) -> AutoRepairClient:
        coordinator = object.__new__(AutoRepairClient)
        coordinator._log = lambda *_: None
        next_worker_id = 0

        def create_worker() -> _FakeWorker:
            nonlocal next_worker_id
            next_worker_id += 1
            return _FakeWorker(next_worker_id, tracker)

        coordinator.create_package_worker = create_worker
        return coordinator

    async def test_case_concurrency_is_bounded_and_workers_are_isolated(self) -> None:
        tracker = _Tracker()
        coordinator = self._coordinator(tracker)
        packages = [f"case-{index}" for index in range(7)]

        await coordinator.process_packages_concurrently(packages, concurrency=3)

        self.assertEqual(tracker.max_active, 3)
        self.assertCountEqual(tracker.started, packages)
        self.assertCountEqual(tracker.finished, packages)
        self.assertEqual(len(set(tracker.worker_ids)), len(packages))
        self.assertCountEqual(tracker.cleaned_worker_ids, tracker.worker_ids)

    async def test_concurrency_one_remains_serial(self) -> None:
        tracker = _Tracker()
        coordinator = self._coordinator(tracker)

        await coordinator.process_packages_concurrently(
            ["case-a", "case-b", "case-c"], concurrency=1
        )

        self.assertEqual(tracker.max_active, 1)

    async def test_invalid_runtime_concurrency_is_rejected(self) -> None:
        coordinator = self._coordinator(_Tracker())
        with self.assertRaisesRegex(ValueError, "at least 1"):
            await coordinator.process_packages_concurrently(["case"], concurrency=0)

    async def test_one_worker_initialization_failure_does_not_cancel_others(self) -> None:
        tracker = _Tracker()
        coordinator = self._coordinator(tracker)
        original_factory = coordinator.create_package_worker
        calls = 0

        def flaky_factory() -> _FakeWorker:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("worker setup failed")
            return original_factory()

        coordinator.create_package_worker = flaky_factory
        await coordinator.process_packages_concurrently(
            ["case-a", "case-b", "case-c"], concurrency=3
        )

        self.assertCountEqual(tracker.finished, ["case-a", "case-c"])
        self.assertEqual(len(tracker.cleaned_worker_ids), 2)

    def test_cli_concurrency_and_paths(self) -> None:
        args = parse_args(
            [
                "--concurrency",
                "3",
                "--base-dir",
                "/cases",
                "--run-dir",
                "/runs/test",
            ]
        )
        self.assertEqual(args.concurrency, 3)
        self.assertEqual(args.base_dir, "/cases")
        self.assertEqual(args.run_dir, "/runs/test")

    def test_cli_rejects_zero_concurrency(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--concurrency", "0"])

    def test_package_discovery_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "case-z").mkdir()
            (root / "case-a").mkdir()
            (root / "README.txt").write_text("not a case", encoding="utf-8")
            coordinator = object.__new__(AutoRepairClient)
            coordinator.base_dir = str(root)

            self.assertEqual(
                coordinator.discover_packages(), ["case-a", "case-z"]
            )


if __name__ == "__main__":
    unittest.main()
