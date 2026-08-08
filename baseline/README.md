# Baseline

## Current Baseline

The repository preserves the existing paper-style **MCP + Tools baseline**:

- `client.py` uses the full-file generation strategy.
- `client_patch.py` uses the patch-generation strategy.
- `server.py` and `server_patch.py` expose the MCP tools.
- `automation/parallel_agent_launcher.py` isolates Case workspaces and caps
  concurrency at two.

The official baseline must be run only after the dataset, Case schema,
validator image, model, resource limits, and evaluation protocol are frozen.
This repository therefore provides a validated plan template, but does not
ship experiment outputs.

## Plan Template

Start with [`plan.example.yaml`](plan.example.yaml). Replace every
`/absolute/path/...` value with paths on the runner. Each job must expose one
canonical Case directory and a unique run root.

Dry-run validation:

```bash
uv run python automation/parallel_agent_launcher.py \
  --plan baseline/plan.example.yaml
```

Add `--execute` only for an approved run. Keep the generated plan and results
outside Git, or store them in the team's artifact repository.

## Future Model-Only Baseline

A model-only baseline without MCP tools is a separate experiment. It must
define its own prompt, Case input contract, output format, three-repair-round
policy, and Validator handoff before implementation. Until those are frozen,
do not label an MCP run as model-only.
