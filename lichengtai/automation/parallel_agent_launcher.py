from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import yaml


REPO = Path(__file__).resolve().parents[1]
PERSONAL_BASE = Path(
    os.environ.get("BUILD_BENCH_WORKSPACE_ROOT", str(REPO.parent))
).expanduser().resolve()

MAX_PARALLEL_LIMIT = 2

JOB_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
)


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    config_path: Path
    run_root: Path
    base_dir: Path
    model: str | None
    timeout_seconds: int
    experiment_group: str


@dataclass(frozen=True)
class LaunchPlan:
    plan_path: Path
    max_parallel: int
    client_script: Path
    jobs: tuple[JobSpec, ...]


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _resolved(
    value: str,
    relative_to: Path,
) -> Path:
    path = Path(value).expanduser()

    if not path.is_absolute():
        path = relative_to / path

    return path.resolve()


def _load_mapping(
    path: Path,
    label: str,
) -> dict:
    try:
        value = yaml.safe_load(
            path.read_text(encoding="utf-8")
        ) or {}
    except OSError as error:
        raise RuntimeError(
            f"{label} could not be read: {path}"
        ) from error
    except yaml.YAMLError as error:
        raise RuntimeError(
            f"{label} is invalid YAML: {path}"
        ) from error

    if not isinstance(value, dict):
        raise RuntimeError(
            f"{label} must be a mapping: {path}"
        )

    return dict(value)


def load_plan(
    plan_path: Path,
    *,
    personal_base: Path = PERSONAL_BASE,
    repo_root: Path = REPO,
) -> LaunchPlan:
    plan_path = plan_path.expanduser().resolve()
    personal_base = personal_base.expanduser().resolve()
    repo_root = repo_root.expanduser().resolve()

    if not _within(personal_base, plan_path):
        raise RuntimeError(
            "Plan is outside personal workspace: "
            f"{plan_path}"
        )

    raw = _load_mapping(
        plan_path,
        "Launch plan",
    )

    if (
        str(raw.get("schema_version", "")).strip()
        != "0.1"
    ):
        raise RuntimeError(
            "schema_version must be '0.1'"
        )

    max_parallel = raw.get(
        "max_parallel",
        2,
    )

    if (
        isinstance(max_parallel, bool)
        or not isinstance(max_parallel, int)
        or not 1
        <= max_parallel
        <= MAX_PARALLEL_LIMIT
    ):
        raise RuntimeError(
            "max_parallel must be an integer "
            "from 1 to 2"
        )

    client_script = _resolved(
        str(
            raw.get("client_script")
            or repo_root / "client_patch.py"
        ),
        plan_path.parent,
    )

    if not client_script.is_file():
        raise RuntimeError(
            "Client script does not exist: "
            f"{client_script}"
        )

    if not _within(
        personal_base,
        client_script,
    ):
        raise RuntimeError(
            "Client script is outside personal "
            f"workspace: {client_script}"
        )

    raw_jobs = raw.get("jobs")

    if (
        not isinstance(raw_jobs, list)
        or not raw_jobs
    ):
        raise RuntimeError(
            "jobs must be a non-empty list"
        )

    jobs: list[JobSpec] = []
    ids: set[str] = set()
    run_roots: set[Path] = set()
    base_dirs: set[Path] = set()

    for index, raw_job in enumerate(
        raw_jobs,
        1,
    ):
        if not isinstance(raw_job, dict):
            raise RuntimeError(
                f"jobs[{index}] must be a mapping"
            )

        job_id = str(
            raw_job.get("job_id", "")
        ).strip()

        if not JOB_ID_RE.fullmatch(job_id):
            raise RuntimeError(
                f"Invalid job_id: {job_id!r}"
            )

        if job_id in ids:
            raise RuntimeError(
                f"Duplicate job_id: {job_id}"
            )

        ids.add(job_id)

        config_value = str(
            raw_job.get("config_path", "")
        ).strip()

        run_value = str(
            raw_job.get("run_root", "")
        ).strip()

        if not config_value or not run_value:
            raise RuntimeError(
                f"jobs[{index}] requires "
                "config_path and run_root"
            )

        config_path = _resolved(
            config_value,
            plan_path.parent,
        )

        run_root = _resolved(
            run_value,
            plan_path.parent,
        )

        if not config_path.is_file():
            raise RuntimeError(
                "Config does not exist: "
                f"{config_path}"
            )

        if not _within(
            personal_base,
            config_path,
        ):
            raise RuntimeError(
                "Config is outside personal "
                f"workspace: {config_path}"
            )

        if not _within(
            personal_base,
            run_root,
        ):
            raise RuntimeError(
                "Run root is outside personal "
                f"workspace: {run_root}"
            )

        if run_root in {
            personal_base,
            repo_root,
        }:
            raise RuntimeError(
                f"Unsafe run root: {run_root}"
            )

        if run_root in run_roots:
            raise RuntimeError(
                f"Duplicate run_root: {run_root}"
            )

        run_roots.add(run_root)

        config = _load_mapping(
            config_path,
            "Agent config",
        )

        paths = config.get("paths")

        if not isinstance(paths, dict):
            raise RuntimeError(
                "Config paths must be a mapping: "
                f"{config_path}"
            )

        base_value = paths.get("base_dir")

        if (
            not isinstance(base_value, str)
            or not base_value.strip()
        ):
            raise RuntimeError(
                "paths.base_dir is missing: "
                f"{config_path}"
            )

        base_dir = _resolved(
            base_value,
            repo_root,
        )

        if not base_dir.is_dir():
            raise RuntimeError(
                f"base_dir does not exist: {base_dir}"
            )

        packages = [
            entry
            for entry in base_dir.iterdir()
            if entry.is_dir()
        ]

        if len(packages) != 1:
            raise RuntimeError(
                "Each parallel job must expose "
                "exactly one Case directory; "
                f"{base_dir} contains "
                f"{len(packages)}"
            )

        if base_dir in base_dirs:
            raise RuntimeError(
                f"Duplicate base_dir: {base_dir}"
            )

        base_dirs.add(base_dir)

        model_value = raw_job.get("model")

        model = (
            None
            if model_value is None
            else str(model_value).strip()
        )

        if (
            model_value is not None
            and not model
        ):
            raise RuntimeError(
                f"jobs[{index}].model "
                "cannot be empty"
            )

        timeout_seconds = raw_job.get(
            "timeout_seconds",
            14400,
        )

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(
                timeout_seconds,
                int,
            )
            or not 60
            <= timeout_seconds
            <= 86400
        ):
            raise RuntimeError(
                f"jobs[{index}].timeout_seconds "
                "must be 60..86400"
            )

        experiment_group = str(
            raw_job.get("experiment_group")
            or f"parallel-v1-{job_id}"
        ).strip()

        jobs.append(
            JobSpec(
                job_id=job_id,
                config_path=config_path,
                run_root=run_root,
                base_dir=base_dir,
                model=model,
                timeout_seconds=timeout_seconds,
                experiment_group=(
                    experiment_group
                ),
            )
        )

    return LaunchPlan(
        plan_path=plan_path,
        max_parallel=max_parallel,
        client_script=client_script,
        jobs=tuple(jobs),
    )


def build_job_environment(
    job: JobSpec,
    inherited: dict[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(
        os.environ
        if inherited is None
        else inherited
    )

    # Local parallel runs must not share an
    # official Runner output location.
    environment.pop(
        "BB_WORKSPACE",
        None,
    )

    environment.pop(
        "BUILD_BENCH_RESULT_ROOT",
        None,
    )

    environment.pop(
        "BUILD_BENCH_STAGING_ROOT",
        None,
    )

    environment["BB_AGENT_CONFIG"] = str(
        job.config_path
    )

    environment["BB_RUN_ROOT"] = str(
        job.run_root
    )

    environment[
        "BUILD_BENCH_EXPERIMENT_GROUP"
    ] = job.experiment_group

    environment[
        "PYTHONDONTWRITEBYTECODE"
    ] = "1"

    if job.model is not None:
        environment["LLM_MODEL"] = job.model

    return environment


def _assert_run_root_ready(
    run_root: Path,
) -> None:
    """Allow historical run-01/prep, but no prior runtime outputs."""

    if not run_root.exists():
        return

    if not run_root.is_dir():
        raise RuntimeError(
            f"Run root is not a directory: {run_root}"
        )

    entries = sorted(
        run_root.iterdir(),
        key=lambda path: path.name,
    )

    unexpected = [
        entry
        for entry in entries
        if entry.name != "prep"
    ]

    if unexpected:
        names = ", ".join(
            entry.name
            for entry in unexpected
        )

        raise RuntimeError(
            "Run root already contains runtime output; "
            f"refusing to overwrite {run_root}: {names}"
        )

    prep = run_root / "prep"

    if prep.exists() and not prep.is_dir():
        raise RuntimeError(
            f"Historical prep path is not a directory: {prep}"
        )


def _write_json(
    path: Path,
    payload: dict,
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}"
    )

    try:
        temporary.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        os.replace(
            temporary,
            path,
        )
    finally:
        temporary.unlink(
            missing_ok=True
        )


async def _stop_group(
    process: asyncio.subprocess.Process,
) -> None:
    if process.returncode is not None:
        return

    try:
        os.killpg(
            process.pid,
            signal.SIGTERM,
        )
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=10,
        )
    except asyncio.TimeoutError:
        try:
            os.killpg(
                process.pid,
                signal.SIGKILL,
            )
        except ProcessLookupError:
            pass

        await process.wait()


async def run_job(
    job: JobSpec,
    *,
    client_script: Path,
    repo_root: Path = REPO,
) -> dict:
    _assert_run_root_ready(job.run_root)

    job.run_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    console_path = (
        job.run_root
        / "launcher-console.log"
    )

    result_path = (
        job.run_root
        / "launcher-job-result.json"
    )

    started = time.time()

    with console_path.open(
        "ab",
        buffering=0,
    ) as console:
        process = (
            await asyncio.create_subprocess_exec(
                sys.executable,
                str(client_script),
                cwd=str(repo_root),
                env=build_job_environment(job),
                stdout=console,
                stderr=(
                    asyncio.subprocess.STDOUT
                ),
                start_new_session=True,
            )
        )

        state = "completed"

        try:
            return_code = (
                await asyncio.wait_for(
                    process.wait(),
                    timeout=(
                        job.timeout_seconds
                    ),
                )
            )
        except asyncio.TimeoutError:
            state = "timeout"

            await _stop_group(process)

            return_code = process.returncode

        except asyncio.CancelledError:
            await _stop_group(process)
            raise

    payload = {
        "schema_version": "0.1",
        "job_id": job.job_id,
        "state": state,
        "return_code": return_code,
        "config_path": str(
            job.config_path
        ),
        "run_root": str(job.run_root),
        "base_dir": str(job.base_dir),
        "console_log": str(console_path),
        "started_at_epoch": started,
        "finished_at_epoch": time.time(),
    }

    _write_json(
        result_path,
        payload,
    )

    return payload


Runner = Callable[
    [JobSpec],
    Awaitable[dict],
]


async def execute_plan(
    plan: LaunchPlan,
    runner: Runner | None = None,
) -> list[dict]:
    semaphore = asyncio.Semaphore(
        plan.max_parallel
    )

    async def guarded(
        job: JobSpec,
    ) -> dict:
        async with semaphore:
            if runner is not None:
                return await runner(job)

            return await run_job(
                job,
                client_script=(
                    plan.client_script
                ),
            )

    tasks = [
        asyncio.create_task(
            guarded(job),
            name=job.job_id,
        )
        for job in plan.jobs
    ]

    try:
        return list(
            await asyncio.gather(*tasks)
        )
    except BaseException:
        for task in tasks:
            task.cancel()

        await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

        raise


def dry_run_payload(
    plan: LaunchPlan,
) -> dict:
    return {
        "schema_version": "0.1",
        "mode": "dry-run",
        "max_parallel": (
            plan.max_parallel
        ),
        "client_script": str(
            plan.client_script
        ),
        "jobs": [
            {
                "job_id": job.job_id,
                "config_path": str(
                    job.config_path
                ),
                "run_root": str(
                    job.run_root
                ),
                "base_dir": str(
                    job.base_dir
                ),
                "model": (
                    job.model
                    or "<inherited>"
                ),
                "timeout_seconds": (
                    job.timeout_seconds
                ),
            }
            for job in plan.jobs
        ],
        "model_started": False,
        "api_called": False,
        "docker_started": False,
    }


async def _main_async(
    arguments: argparse.Namespace,
) -> int:
    plan = load_plan(
        Path(arguments.plan)
    )

    if not arguments.execute:
        print(
            json.dumps(
                dry_run_payload(plan),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )

        return 0

    results = await execute_plan(plan)

    summary = {
        "schema_version": "0.1",
        "plan": str(plan.plan_path),
        "max_parallel": (
            plan.max_parallel
        ),
        "jobs": results,
        # This means the launcher processes
        # completed. It does not mean every
        # package build succeeded.
        "all_jobs_completed": all(
            result.get("state")
            == "completed"
            for result in results
        ),
    }

    summary_path = (
        plan.plan_path.with_name(
            f"{plan.plan_path.stem}"
            "-launcher-summary.json"
        )
    )

    _write_json(
        summary_path,
        summary,
    )

    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )

    return (
        0
        if summary["all_jobs_completed"]
        else 1
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Launch isolated Build-Bench "
            "Agent jobs with a hard "
            "concurrency cap of two."
        )
    )

    parser.add_argument(
        "--plan",
        required=True,
    )

    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Start model/API/MCP/Docker work. "
            "Without this flag only plan "
            "validation is performed."
        ),
    )

    return asyncio.run(
        _main_async(
            parser.parse_args()
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
