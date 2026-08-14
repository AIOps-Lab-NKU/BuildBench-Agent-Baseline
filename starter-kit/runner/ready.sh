#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
source "$ROOT/runner/result-json.sh"

usage='Usage: ./bb ready --agent PATH [--output FILE.zip] [--json]'
agent_dir=""
output="$ROOT/dist/agent-submission.zip"
json_mode=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent)
      [[ $# -ge 2 ]] || fail "$usage" 2
      agent_dir="$2"
      shift 2
      ;;
    --output)
      [[ $# -ge 2 ]] || fail "$usage" 2
      output="$2"
      shift 2
      ;;
    --json)
      json_mode=true
      shift
      ;;
    *)
      fail "$usage" 2
      ;;
  esac
done

[[ -n "$agent_dir" ]] || fail "$usage" 2
if [[ ! -d "$agent_dir" ]]; then
  if [[ "$json_mode" == true ]]; then
    bb_emit_result ready failed 4 \
      "Agent readiness checks did not run." \
      "Agent directory does not exist: $agent_dir" \
      "" "" "" "" "./bb bootstrap my-agent --json"
    exit 4
  fi
  fail "Agent directory does not exist: $agent_dir" 4
fi
[[ "$output" == *.zip ]] || fail "Package output must end in .zip" 2

agent_dir="$(cd "$agent_dir" && pwd)"
mkdir -p "$(dirname "$output")"
output="$(cd "$(dirname "$output")" && pwd)/$(basename "$output")"
case "$output" in
  "$ROOT"/*) ;;
  *) fail "Ready output must stay inside the Starter Kit directory" 2 ;;
esac

display_agent="$agent_dir"
if [[ "$agent_dir" == "$ROOT"/* ]]; then
  display_agent="${agent_dir#"$ROOT"/}"
fi
display_output="${output#"$ROOT"/}"
run_id="${BB_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
ready_root="$ROOT/runs/ready/$run_id"
snapshot="$ready_root/agent"
result_file="$ready_root/result.json"
mkdir -p "$snapshot"
cp -a "$agent_dir/." "$snapshot/"
trap 'rm -rf -- "$snapshot"' EXIT

# Never leave an older package at the requested path after a failed readiness run.
rm -f -- "$output"

run_step() {
  if [[ "$json_mode" == true ]]; then
    "$@" 1>&2
  else
    "$@"
  fi
}

write_failure() {
  local exit_code="$1"
  local summary="$2"
  local diagnostic="$3"
  local next_command="$4"
  bb_emit_result ready failed "$exit_code" "$summary" "$diagnostic" \
    "" "" "" "" "$next_command" >"$result_file"
  if [[ "$json_mode" == true ]]; then
    cat "$result_file"
  else
    fail "$diagnostic" "$exit_code"
  fi
  exit "$exit_code"
}

set +e
run_step "$ROOT/runner/doctor.sh" --quiet
step_exit=$?
set -e
[[ $step_exit -eq 0 ]] || write_failure 3 \
  "Local environment checks failed." \
  "Fix the Docker or image error printed above, then run ready again." \
  "./bb doctor"

set +e
run_step "$ROOT/runner/check-agent.sh" --agent "$snapshot"
step_exit=$?
set -e
[[ $step_exit -eq 0 ]] || write_failure 4 \
  "Agent submission checks failed." \
  "Fix the manifest, files, dependencies, or prohibited content reported above." \
  "./bb check --agent ./$display_agent"

set +e
run_step "$ROOT/runner/test-agent.sh" --agent "$snapshot"
step_exit=$?
set -e
[[ $step_exit -eq 0 ]] || write_failure 5 \
  "One or more local Example Cases failed." \
  "Inspect the Summary and Progress paths printed above before changing the Agent." \
  "./bb test --agent ./$display_agent"

set +e
run_step "$ROOT/runner/package-agent.sh" --agent "$snapshot" --output "$output"
step_exit=$?
set -e
[[ $step_exit -eq 0 && -f "$output" ]] || write_failure 6 \
  "Submission packaging failed." \
  "No current submission ZIP was published; inspect the packaging error above." \
  "./bb package --agent ./$display_agent"

digest="$(sha256sum "$output" | awk '{print $1}')"
bytes="$(wc -c <"$output" | tr -d '[:space:]')"
bb_emit_result ready succeeded 0 \
  "Agent passed local readiness checks and was packaged." "" \
  "agent_submission" "$display_output" "$digest" "$bytes" "" >"$result_file"

if [[ "$json_mode" == true ]]; then
  cat "$result_file"
else
  printf '\n'
  ok "Agent is ready to upload"
  info "Package: $display_output"
  info "SHA256: $digest"
  info "Result:  ${result_file#"$ROOT"/}"
fi
