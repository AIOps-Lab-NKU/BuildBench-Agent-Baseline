# BuildBench Agent and Baseline Code Handoff

This repository is the code-only handoff for the current BuildBench Agent
working version. It is intended for the `AIOps-Lab-NKU` organization and
excludes experiment artifacts and private infrastructure.

## Entry files

Current experimental MCP Agent:
- client_patch.py
- server_patch.py

Original/reference files retained for comparison:
- client.py
- server.py

## Included components

- MCP Agent client/server
- DEB/RPM package-aware source handling
- Docker/OBS build backend code
- Agent workspace handling
- runtime configuration/isolation support
- result/Token/tool/build observability
- parallel launcher
- prompts
- current regression and contract tests

## Baseline boundary

The repository preserves the existing MCP + Tools baseline and documents its
run contract under `baseline/`. A model-only baseline without MCP tools is a
separate experiment and is intentionally not mislabeled or mixed into these
entry points.

## Notes

This implementation is based on the original Build-Bench MCP Agent and
contains subsequent experimental and engineering modifications. It is not
claimed to be a from-scratch implementation.

Experiment runs, build artifacts, logs, personal planning files, saved
environment files and local virtual environments are intentionally excluded.

config/info.example.yaml is only a template. Local paths and runtime settings
must be supplied for the target environment.
