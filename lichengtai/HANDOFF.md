# Build-Bench MCP Agent Code Handoff

This directory is a snapshot of the current Build-Bench MCP Agent working
version used in the recent experiments.

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

## Notes

This implementation is based on the original Build-Bench MCP Agent and
contains subsequent experimental and engineering modifications. It is not
claimed to be a from-scratch implementation.

Experiment runs, build artifacts, logs, personal planning files, saved
environment files and local virtual environments are intentionally excluded.

config/info.example.yaml is only a template. Local paths and runtime settings
must be supplied for the target environment.
