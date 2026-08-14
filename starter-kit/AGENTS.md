# Build-Bench coding-agent guide

## Start here

Build-Bench evaluates a submitted **Repair Agent** on reproducible software-package build failures. You are a participant-side coding assistant helping create that Repair Agent; you are not the Agent that the organizer will evaluate.

Your deliverable is `dist/agent-submission.zip`. It must contain the Repair Agent source, `agent.yaml`, and exactly pinned Python dependencies. The organizer runs the immutable ZIP on separate Cases and generates the canonical repair patch from the Agent's modified worktree.

Do not request hidden Cases, production credentials, Docker Socket access, organizer-only Validator configuration, or reference repairs.

## Fast autonomous workflow

From the Starter Kit root:

```bash
./bb bootstrap my-agent --json
```

Read the JSON result. On success, implement the Repair Agent only in:

```text
agents/my-agent/src/
```

Change `requirements.lock` only when dependencies change, and pin every dependency with `==`. Change `agent.yaml` only when the Agent name, version, runtime profile, entrypoint, or protocol declaration changes.

When the implementation is ready:

```bash
./bb ready --agent ./agents/my-agent --json
```

Do not claim completion unless the result has `"status": "succeeded"`, the ZIP exists, and its SHA-256 matches the returned artifact metadata.

## Repository map

```text
bb                         public command entrypoint
agents/example-agent/      read-only reference implementation
agents/my-agent/           participant Repair Agent workspace
example-cases/             released local examples only
runner/                    Starter Kit runtime; do not customize for a submission
templates/                 official managed-Python template
runs/                      disposable local results
dist/                      generated submission archive
```

Do not modify `runner/`, `templates/`, `example-cases/`, or the top-level `bb` script to make an Agent pass. Those files are not included in the submission and formal evaluation uses organizer-controlled copies.

## Repair Agent contract

The managed-Python Agent ZIP contains:

```text
agent.yaml
requirements.lock
src/
README.md
```

At runtime the Agent receives:

```text
/workspace/input/          read-only Case metadata and initial failure evidence
/workspace/work/repo/      writable package worktree; modify only this repository
/workspace/output/         writable structured Agent output
```

The Agent entrypoint is taken from `agent.yaml`. It must be deterministic and non-interactive. It may write `/workspace/output/agent-result.json`; stdout and stderr are captured by the platform. The Agent must not start Docker or contact the Docker Validator directly.

A minimal result is:

```json
{
  "schema_version": "0.1",
  "status": "completed"
}
```

The platform derives the canonical patch from changes under `/workspace/work/repo`. Formal success is determined by applying that patch to a clean Case and completing the target build, not by comparing text with a reference patch.

## Command contract

Use the high-level commands for autonomous work:

- `./bb bootstrap NAME --json` prepares the environment and creates an Agent workspace.
- `./bb ready --agent PATH --json` snapshots, checks, tests, and packages one exact Agent version.

JSON command results use this envelope:

```json
{
  "schema_version": "0.1",
  "command": "ready",
  "status": "succeeded",
  "exit_code": 0,
  "summary": "...",
  "diagnostics": [],
  "artifacts": [],
  "next_actions": []
}
```

In JSON mode, stdout contains the result document and detailed progress is written to stderr. Treat a nonzero process exit as failure even if a previous ZIP still exists.

Granular commands remain available for diagnosis:

```bash
./bb doctor
./bb demo
./bb init my-agent
./bb check --agent ./agents/my-agent
./bb test --agent ./agents/my-agent
./bb package --agent ./agents/my-agent
```

## Prohibited submission content

Never include:

- `.env` files, credentials, API keys, or private keys;
- hidden Case information or reference repairs;
- pre-generated `repair.diff` files;
- caches, virtual environments, model caches, `runs/`, or prior build output;
- symlinks or files outside the documented ZIP root;
- unpinned or URL-based Python requirements.

## Failure recovery

- Environment failure: run `./bb doctor` and fix the reported Docker/image problem.
- Existing Agent name: inspect the current directory; use another name or use `--force` only when replacing it is intentional.
- Submission check failure: correct the exact manifest, file, dependency, or secret diagnostic.
- Example Case failure: inspect the printed `summary.json`, `progress.log`, and per-Case `build.log` before editing.
- Packaging failure: confirm checks pass and that the output stays under the Starter Kit directory.

When reporting work to the participant, state which files changed, which checks ran, whether the Example Cases passed, and the final ZIP path and SHA-256.
