# Agent and Baseline Runbook

## Entry Points

| Entry | Mode | Notes |
| --- | --- | --- |
| `client.py` | Full-file generation | Original MCP+Tools Agent |
| `client_patch.py` | Patch generation | Current experimental Agent |
| `server.py` | Full-file MCP server | Starts through the client |
| `server_patch.py` | Patch MCP server | Starts through the patch client |
| `automation/parallel_agent_launcher.py` | Campaign launcher | Isolates jobs and limits concurrency |

The launcher removes shared result-root variables, assigns each job a unique
run directory, and refuses to overwrite a run that already contains runtime
outputs. It supports at most two concurrent jobs.

## Configuration Order

1. Copy `config/info.example.yaml` to a local, private config.
2. Set `paths.base_dir` to the canonical Case directory.
3. Set `paths.result_dir`, `paths.log_dir`, and `paths.temp_work_dir` to a
   run-specific directory outside Git.
4. Choose `build.backend` as `obs` or `docker`.
5. Set `API_KEY`, `API_BASE_URL`, and `LLM_MODEL` in `.env`.

The client supports `--base-dir`, `--run-dir`, `--concurrency`,
`--max-build-attempts`, and `--max-tool-rounds` in the full-file path.

## Baseline Boundary

The current reproducible baseline is MCP + Tools. A future model-only baseline
must be implemented and evaluated separately; it must not reuse MCP tool
results or be reported as the same experiment. Formal batch runs wait until
the Case set and validator protocol are frozen.

## Smoke Test

Use one canonical Case and one isolated run root first:

```bash
BUILD_BENCH_BACKEND=docker \
uv run python client_patch.py \
  --base-dir /absolute/path/to/one-case \
  --run-dir /absolute/path/to/run/smoke
```

Review `result_text_res/`, `result_log_res/`, and the Docker result directory
before increasing concurrency.
