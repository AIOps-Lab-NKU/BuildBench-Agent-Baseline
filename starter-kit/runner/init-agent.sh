#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

[[ $# -eq 1 ]] || fail "Usage: ./bb init NAME" 2
name="$1"

target="$(
  run_managed_python \
    -m runner.init_agent \
    "$name" \
    --template "$ROOT/templates/managed-python-agent" \
    --target-root "$ROOT/agents"
)"

ok "Agent created"
info "$target"
printf '\n'
printf 'Edit:  %s/src/\n' "$target"
printf 'Check: ./bb check --agent ./agents/%s\n' "$name"
printf 'Test:  ./bb test --agent ./agents/%s\n' "$name"
