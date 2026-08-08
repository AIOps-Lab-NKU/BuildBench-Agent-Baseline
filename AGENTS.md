# Repository Guidelines

## Scope

This repository owns the BuildBench Agent, MCP servers, build backends,
parallel launcher, and baseline run documentation. It does not own the
1500+ case archive, Docker cache, server credentials, or raw build logs.

## Layout

- `client.py`, `client_patch.py`: Agent entry points.
- `server.py`, `server_patch.py`: MCP servers.
- `tools/`: build backends and Agent tools.
- `automation/`: isolated multi-Case launch utilities.
- `baseline/`: baseline contract and run-plan examples.
- `tests/`: offline `unittest` coverage.

## Development

Use Python 3.11+ and the locked `uv` environment:

```bash
uv sync
uv run python -m unittest discover -s tests -v
```

Run plan validation without starting an Agent:

```bash
uv run python automation/parallel_agent_launcher.py \
  --plan baseline/plan.example.yaml
```

Keep changes focused and preserve the existing configuration-driven backend
selection. Use `snake_case` for Python names and clear imperative commit
subjects.

## Security

Never commit `.env`, API keys, server passwords, private paths, raw logs, or
large case artifacts. Use `config/info.example.yaml` and `.env.example` for
placeholders. Docker validation is for trusted organizer-controlled runs only.
