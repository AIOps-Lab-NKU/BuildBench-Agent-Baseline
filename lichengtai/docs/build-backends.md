# Build validation backends

Build-Bench now exposes one Agent-facing MCP tool:

```text
run_build_validation_tool(package_path)
```

The tool delegates to either the original online OBS workflow or the local
Docker Validator. The LLM, MCP file tools, temporary workspace and iterative
repair loop are shared by both backends.

## Select a backend

The compatibility default is configured in `config/info.yaml`:

```yaml
build:
  backend: obs
```

For one Docker run, prefer an environment override:

```bash
cd /path/to/Build-bench
BUILD_BENCH_BACKEND=docker uv run python client.py
```

For concurrent Case-level Agent repair, use the built-in CLI. No helper script
or YAML edit is required:

```bash
cd /path/to/Build-bench
BUILD_BENCH_BACKEND=docker \
LLM_MODEL=qwen3.7-max \
.venv/bin/python client.py \
  --base-dir /absolute/path/to/cases \
  --run-dir /absolute/path/to/run \
  --concurrency 3
```

`--concurrency` controls how many different Cases can run at once. Each Case
gets a dedicated `AutoRepairClient`, LLM client, MCP server/session and package
state. Repair attempts inside one Case remain sequential because each attempt
depends on the previous build log. The default is `--concurrency 1`, which
preserves serial behavior.

`--run-dir` isolates all outputs from one campaign:

```text
run/
├── result_log_res/
├── result_text_res/
├── temp_workspace/
├── docker-builds/
└── staging/
```

Use `client_patch.py` instead for the existing patch-generation strategy.
Removing the environment variable returns to the configured OBS backend.

Paths can also be overridden without editing YAML by setting
`BUILD_BENCH_VALIDATOR_COMMAND`, `BUILD_BENCH_CASE_STORE`,
`BUILD_BENCH_RESULT_ROOT`, or `BUILD_BENCH_STAGING_ROOT`.

## Docker configuration

```yaml
build:
  backend: obs
  client_tool_timeout_seconds: 7200
  docker:
    validator_command: /path/to/docker-validator/bin/build-case-docker
    case_store_dir: /absolute/path/to/cases
    result_root: run_records/docker-builds
    staging_root: temp_workspace/docker-cases
    case_map: {}
```

`base_dir` may point directly to a directory of canonical Cases. Each Case is
expected to contain:

```text
case-id/
├── manifest.json
├── input/
├── config/buildconfig
└── dependencies/...
```

During initialization, Build-Bench copies only `input/` into the flat Agent
workspace and copies an initial failure log to `log_failed.txt`. It records the
read-only canonical Case path in `.buildbench-case.json`.

For a legacy flat Build-Bench package, add an explicit mapping without moving
the original package:

```yaml
case_map:
  texmath: /absolute/path/to/canonical/texmath-case
```

## Per-attempt behavior

For every validation attempt, the Docker adapter:

1. creates an isolated temporary canonical Case;
2. reuses immutable config and RPM dependencies with hard links when possible;
3. replaces only `input/` with the current Agent workspace;
4. calls `build-case-docker`;
5. saves `build-result.json`, `build.log` and RPM/SRPM artifacts under a unique
   result directory;
6. copies a failed `build.log` to the Agent workspace as `log_failed.txt` for
   the next repair round;
7. removes the temporary Case without modifying the canonical Case.

Results are stored as:

```text
run_records/docker-builds/<package>/<timestamp-id>/
├── build-result.json
├── build.log
└── artifacts/
```

The normalized MCP result preserves the Validator statuses: `succeeded`,
`failed`, `unresolvable`, `timeout`, `invalid_patch`, and
`infrastructure_error`.

## Safety boundary

The current Validator uses `docker:privileged` and is suitable only for trusted
organizer-controlled baseline runs. It is not a sandbox for untrusted
participant code or patches.

## Tests

```bash
cd /path/to/Build-bench
.venv/bin/python -m unittest -v \
  tests.test_build_backends \
  tests.test_client_build_flow \
  tests.test_client_concurrency
```

The Docker Validator keeps its own independent test suite.
