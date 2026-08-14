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
      fail "Usage: ./bb test --agent PATH" 2
      ;;
  esac
done
[[ -n "$agent_dir" ]] || fail "Usage: ./bb test --agent PATH" 2

"$ROOT/runner/doctor.sh" --quiet
AGENT_DIR="$(cd "$agent_dir" && pwd)"
agent_name="$(
  run_managed_python_with_mount "$AGENT_DIR" \
    "$ROOT/runner/validate_agent.py" \
    "$AGENT_DIR/agent.yaml" \
    --agent-name
)"
run_id="${BB_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
run_root="$ROOT/runs/tests/$agent_name/$run_id"
mkdir -p "$run_root/cases"
progress_log="$run_root/progress.log"

mapfile -t cases < <(
  sed -e 's/[[:space:]]*#.*$//' -e '/^[[:space:]]*$/d' \
    "$ROOT/example-cases/cases.txt"
)
[[ ${#cases[@]} -gt 0 ]] || fail "No local Example Cases are configured."

log() {
  printf '%s\n' "$1" | tee -a "$progress_log"
}

log "Agent: $agent_name"
log "Cases: ${#cases[@]}"
success_count=0
for index in "${!cases[@]}"; do
  case_id="${cases[$index]}"
  log "[$((index + 1))/${#cases[@]}] Testing $case_id"
  set +e
  "$ROOT/runner/run-agent-case.sh" \
    --agent "$AGENT_DIR" \
    --case "$case_id" \
    --output "$run_root/cases/$case_id" \
    2>&1 | tee -a "$progress_log"
  case_exit=${PIPESTATUS[0]}
  set -e
  if [[ $case_exit -eq 0 ]]; then
    success_count=$((success_count + 1))
    log "[$((index + 1))/${#cases[@]}] $case_id succeeded"
  else
    log "[$((index + 1))/${#cases[@]}] $case_id failed (exit=$case_exit)"
  fi
done

summary_args=()
for case_id in "${cases[@]}"; do
  summary_args+=(--case "$case_id")
done
run_managed_python \
  "$ROOT/runner/summarize_local_test.py" \
  --run-dir "$run_root" \
  --agent-name "$agent_name" \
  "${summary_args[@]}" \
  >/dev/null
printf '%s\n' "$run_root" >"$ROOT/runs/tests/$agent_name/latest-run.txt"

printf '\n'
printf 'Summary: %s\n' "$run_root/summary.json"
printf 'Progress: %s\n' "$progress_log"
printf 'Passed: %d/%d\n' "$success_count" "${#cases[@]}"

[[ $success_count -eq ${#cases[@]} ]] \
  || fail "One or more local Example Cases failed."
