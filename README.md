# BuildBench Agent and Baseline

This repository packages the current BuildBench MCP Agent, build backends,
parallel launcher, and baseline run contract for the AIOps-Lab-NKU team.

## Contents

- `client.py`, `client_patch.py`: full-file and patch Agent entry points
- `server.py`, `server_patch.py`: MCP servers
- `tools/`: Docker, OBS, Debian, RPM, and workspace backends
- `automation/`: isolated multi-Case launcher
- `baseline/`: baseline plan template and run notes
- `docs/`: runbook and baseline boundary notes
- `tests/`: offline regression and contract tests

## Quick Start

```bash
uv sync
cp .env.example .env
```

Then fill `.env` locally with model credentials and run settings. Keep
credentials, case archives, raw logs, and server paths out of Git.

Run the patch Agent:

```bash
BUILD_BENCH_BACKEND=docker uv run python client_patch.py
```

Validate a baseline plan without starting model work:

```bash
uv run python automation/parallel_agent_launcher.py \
  --plan baseline/plan.example.yaml
```

Run the offline tests:

```bash
uv run python -m unittest discover -s tests -v
```

## Baseline Boundary

The committed baseline is the existing MCP + Tools workflow. A model-only
baseline is a separate experiment and must be defined before it is reported
as such.
