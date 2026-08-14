#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

agent_dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent)
      [[ $# -ge 2 ]] || fail "--agent requires a directory" 2
      agent_dir="$2"
      shift 2
      ;;
    *)
      fail "Usage: ./bb check --agent PATH" 2
      ;;
  esac
done
[[ -n "$agent_dir" ]] || fail "Usage: ./bb check --agent PATH" 2

agent_dir="$(cd "$agent_dir" && pwd)"
run_managed_python_with_mount "$agent_dir" \
  -m runner.check_agent \
  --agent "$agent_dir"
ok "Agent submission is valid"
