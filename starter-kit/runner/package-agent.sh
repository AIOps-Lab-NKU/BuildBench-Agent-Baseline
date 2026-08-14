#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

agent_dir=""
output="$ROOT/dist/agent-submission.zip"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent)
      [[ $# -ge 2 ]] || fail "--agent requires a directory" 2
      agent_dir="$2"
      shift 2
      ;;
    --output)
      [[ $# -ge 2 ]] || fail "--output requires a ZIP path" 2
      output="$2"
      shift 2
      ;;
    *)
      fail "Usage: ./bb package --agent PATH [--output FILE.zip]" 2
      ;;
  esac
done
[[ -n "$agent_dir" ]] || fail "Usage: ./bb package --agent PATH [--output FILE.zip]" 2
[[ "$output" == *.zip ]] || fail "Package output must end in .zip" 2

agent_dir="$(cd "$agent_dir" && pwd)"
run_managed_python_with_mount "$agent_dir" \
  -m runner.package_agent \
  --agent "$agent_dir" \
  --output "$output"
ok "Submission package created"
