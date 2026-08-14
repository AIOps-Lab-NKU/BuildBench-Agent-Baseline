#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

[[ $# -eq 0 ]] || fail "demo accepts no additional arguments" 2

"$ROOT/runner/doctor.sh" --quiet
ok "Docker is available"
ok "Images are ready"

AGENT_DIR="$ROOT/agents/example-agent"
archive_demo_run
RUN_DIR="$ROOT/runs/demo"
exec "$ROOT/runner/run-agent-case.sh" \
  --agent "$AGENT_DIR" \
  --case hello \
  --output "$RUN_DIR"
