# Build-Bench Starter Kit

Build-Bench evaluates software-repair Agents against reproducible package-build cases. This Starter Kit provides the local runner, an example Agent, example cases, submission checks, and packaging commands needed to develop an Agent before uploading it to the competition platform.

## Prerequisites

Install only:

- Git
- Docker Engine 24+ or Docker Desktop with Linux containers enabled

You do not need to install Python, RPM tooling, `obs-build`, or package dependencies on the host. The Starter Kit runs those components in organizer-provided containers.

## Quick start

### Coding Agent path

Open this directory in your coding assistant and ask it to read `AGENTS.md` first. The assistant can then use the two high-level commands:

```bash
./bb bootstrap my-agent --json
# Let the coding assistant implement agents/my-agent/src/
./bb ready --agent ./agents/my-agent --json
```

`bootstrap` prepares an editable Agent workspace. `ready` snapshots that Agent, runs the released local tests and submission checks, and creates `dist/agent-submission.zip` only when every step succeeds.

### Human path

```bash
git clone <starter-kit-release-url> buildbench-starter-kit
cd buildbench-starter-kit

./bb doctor
./bb demo
```

`./bb demo` checks Docker, prepares the example Agent and example case, reproduces the initial build failure, runs the Agent, generates a canonical patch, and validates the repaired package.

Successful output points to:

```text
runs/demo/build-result.json
runs/demo/build.log
runs/demo/repair.diff
```

## Create your Agent

```bash
./bb init my-agent
```

This creates:

```text
agents/my-agent/
??? agent.yaml
??? requirements.lock
??? src/
    ??? __init__.py
    ??? main.py
```

Normally you only edit `agents/my-agent/src/`. Update `requirements.lock` when dependencies change, and update `agent.yaml` when the Agent name, version, runtime, or entrypoint changes.

Use this development loop:

```bash
./bb test --agent ./agents/my-agent
./bb check --agent ./agents/my-agent
./bb package --agent ./agents/my-agent
```

The packaged submission is written to:

```text
dist/agent-submission.zip
```

Upload that ZIP on the competition website. The hosted Smoke Test and full evaluation are run by the organizer platform.

## Commands

| Command | Purpose |
|---|---|
| `./bb doctor` | Check Git, Docker, runtime images, host architecture, and bundled assets. |
| `./bb demo` | Run the complete official example from initial failure to successful validation. |
| `./bb bootstrap NAME [--json]` | Check the environment and create a coding-agent-ready workspace. |
| `./bb init NAME` | Create a new managed-Python Agent from the official template. |
| `./bb test --agent PATH` | Run an Agent on the bundled local example case. |
| `./bb check --agent PATH` | Validate the submission structure, manifest, entrypoint, and prohibited content. |
| `./bb package --agent PATH` | Create a deterministic `agent-submission.zip`. |
| `./bb ready --agent PATH [--json]` | Snapshot, check, test, and package one exact Agent version. |
| `./bb version` | Print the Starter Kit version. |

Run `./bb help` or `./bb <command> --help` for command-specific options.

## Submission contents

The managed-Python submission format is:

```text
agent-submission.zip
??? agent.yaml
??? requirements.lock
??? src/
??? README.md            # optional
```

A minimal `agent.yaml` is:

```yaml
schema_version: "0.1"

agent:
  name: "my-agent"
  version: "1.0.0"

runtime:
  type: "managed"
  profile: "python-3.11"

entrypoint:
  - "python"
  - "-m"
  - "src.main"

protocol:
  version: "0.1"
```

Do not include credentials, `.env` files, API keys, hidden-case material, pre-generated repair patches, caches, virtual environments, or prior run outputs.

## Runtime interface

Each Agent instance receives an isolated workspace:

```text
/workspace/
??? input/               # read-only case metadata and initial evidence
??? work/repo/           # writable package worktree
??? output/              # writable structured Agent output
```

The Agent reads `/workspace/input`, edits only `/workspace/work/repo`, and may write `/workspace/output/agent-result.json`. Standard output and standard error are captured by the runner.

During evaluation, build feedback is requested through the platform protocol. The Agent does not receive the Docker socket and does not start the Docker Validator directly. Build attempts, time, CPU, memory, and network access are limited by competition policy.

When the Agent exits, the platform compares the original case with the modified worktree and generates the canonical `repair.diff`. Formal scoring uses only that organizer-generated patch, re-applied to a clean case and validated by the Docker Validator.

## Local outputs

Local commands write disposable data under `runs/` and packaged ZIP files under `dist/`. These directories are not part of a submission.

A validation result includes:

```text
build-result.json    machine-readable status and artifact metadata
build.log            build diagnostics
repair.diff          organizer-compatible canonical patch
artifacts/           RPM/SRPM or other expected package artifacts
```

## Runtime images

The Starter Kit uses organizer-provided runtime images. `./bb doctor` verifies that the required images are available before a run. If your organization uses a registry mirror, the image references can be overridden:

```bash
export BB_AGENT_IMAGE=<managed-python-image>
export BB_VALIDATOR_IMAGE=<validator-image>
export BB_EXAMPLE_ASSETS_IMAGE=<example-assets-image>
./bb doctor
```

The image versions published with a Starter Kit release define the supported local environment. Formal competition results are always produced by the organizer platform using its pinned evaluator version and hidden validation material.

## Troubleshooting

- If Docker is unavailable, start Docker and rerun `./bb doctor`.
- If an image cannot be pulled, verify registry access or configure the organizer-provided mirror.
- If `./bb check` fails, fix the reported file, manifest, entrypoint, or prohibited-content issue before packaging.
- If the local example build fails, inspect the `build.log` path printed by the command.
- If files under `runs/` are owned by another user, confirm that Docker supports bind mounts for the current workspace.
