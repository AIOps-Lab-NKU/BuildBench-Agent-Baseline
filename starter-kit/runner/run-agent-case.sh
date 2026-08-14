#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

agent_dir=""
case_id=""
run_dir=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent)
      [[ $# -ge 2 ]] || fail "--agent requires a directory" 2
      agent_dir="$2"
      shift 2
      ;;
    --case)
      [[ $# -ge 2 ]] || fail "--case requires an Example Case ID" 2
      case_id="$2"
      shift 2
      ;;
    --output)
      [[ $# -ge 2 ]] || fail "--output requires a directory" 2
      run_dir="$2"
      shift 2
      ;;
    *)
      fail "Usage: run-agent-case.sh --agent PATH --case ID --output PATH" 2
      ;;
  esac
done

[[ -n "$agent_dir" && -n "$case_id" && -n "$run_dir" ]] \
  || fail "Usage: run-agent-case.sh --agent PATH --case ID --output PATH" 2
[[ "$case_id" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] \
  || fail "Invalid Example Case ID: $case_id" 2

AGENT_DIR="$(cd "$agent_dir" && pwd)"
if [[ -e "$run_dir" ]]; then
  fail "Run output already exists: $run_dir"
fi
mkdir -p "$run_dir"
RUN_DIR="$(cd "$run_dir" && pwd)"

mapfile -t ENTRYPOINT < <(
  run_managed_python_with_mount "$AGENT_DIR" \
    -m runner.check_agent \
    --agent "$AGENT_DIR" \
    --entrypoint
)
[[ ${#ENTRYPOINT[@]} -gt 0 ]] || fail "Agent entrypoint is empty."
AGENT_RUNTIME_IMAGE="$("$ROOT/runner/prepare-agent-image.sh" "$AGENT_DIR")"
ok "Agent submission is valid"

INTERNAL_DIR="$RUN_DIR/.internal"
ORIGINAL_CASE="$INTERNAL_DIR/case-original"
INITIAL_RESULT="$INTERNAL_DIR/initial-validation"
FINAL_RESULT="$INTERNAL_DIR/final-validation"
WORKSPACE="$INTERNAL_DIR/workspace"

mkdir -p \
  "$ORIGINAL_CASE" \
  "$WORKSPACE/input" \
  "$WORKSPACE/work/repo" \
  "$WORKSPACE/output"

docker run --rm \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -v "$ORIGINAL_CASE:/out" \
  "$BB_EXAMPLE_ASSETS_IMAGE" \
  sh -eu -c '
    source_dir="/opt/buildbench/example-cases/$1"
    test -d "$source_dir"
    cp -a "$source_dir/." /out/
  ' sh "$case_id"

[[ -f "$ORIGINAL_CASE/manifest.json" ]] \
  || fail "Example Case manifest was not extracted: $case_id"
ok "Example Case prepared"

export BUILD_CASE_RUNTIME_IMAGE="$BB_VALIDATOR_IMAGE"
export BUILD_CASE_CLEANUP_IMAGE="$BB_CLEANUP_IMAGE"
export BUILD_CASE_TMP_ROOT="$INTERNAL_DIR/build-tmp"

set +e
"$ROOT/runner/build-case-docker" \
  --input "$ORIGINAL_CASE" \
  --output "$INITIAL_RESULT" \
  >"$INTERNAL_DIR/initial-validator.console.log" 2>&1
initial_exit=$?
set -e

[[ -f "$INITIAL_RESULT/build-result.json" ]] || {
  cat "$INTERNAL_DIR/initial-validator.console.log" >&2
  fail "Initial Validator result is missing."
}
initial_status="$(json_field "$INITIAL_RESULT/build-result.json" status)"
if [[ $initial_exit -ne 1 || "$initial_status" != "failed" ]]; then
  fail "Expected Example Case $case_id to fail before repair; got status=$initial_status exit=$initial_exit."
fi
cp "$INITIAL_RESULT/build-result.json" "$RUN_DIR/initial-build-result.json"
cp "$INITIAL_RESULT/build.log" "$RUN_DIR/initial-build.log"
ok "Initial build failure reproduced"

cp -a "$ORIGINAL_CASE/." "$WORKSPACE/work/repo/"
cp "$RUN_DIR/initial-build.log" "$WORKSPACE/input/initial-build.log"
cat >"$WORKSPACE/input/task.json" <<EOF
{
  "schema_version": "0.1",
  "case_id": "$case_id",
  "worktree": "/workspace/work/repo",
  "initial_build_log": "/workspace/input/initial-build.log"
}
EOF

set +e
docker run --rm \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --cpus "${BB_AGENT_CPUS:-1}" \
  --memory "${BB_AGENT_MEMORY:-1g}" \
  --pids-limit "${BB_AGENT_PIDS_LIMIT:-128}" \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e BB_WORKSPACE=/workspace \
  -e PYTHONPATH=/agent \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  -v "$AGENT_DIR:/agent:ro" \
  -v "$WORKSPACE/input:/workspace/input:ro" \
  -v "$WORKSPACE/work:/workspace/work" \
  -v "$WORKSPACE/output:/workspace/output" \
  -w /agent \
  "$AGENT_RUNTIME_IMAGE" \
  "${ENTRYPOINT[@]}" \
  >"$RUN_DIR/agent.log" 2>&1
agent_exit=$?
set -e

if [[ -f "$WORKSPACE/output/agent-result.json" ]]; then
  cp "$WORKSPACE/output/agent-result.json" "$RUN_DIR/agent-result.json"
fi
if [[ $agent_exit -ne 0 ]]; then
  cat "$RUN_DIR/agent.log" >&2
  fail "Agent exited with code $agent_exit."
fi
[[ -f "$RUN_DIR/agent-result.json" ]] \
  || fail "Agent did not write agent-result.json."
agent_status="$(json_field "$RUN_DIR/agent-result.json" status)"
[[ "$agent_status" == "completed" ]] \
  || fail "Agent returned status=$agent_status."
ok "Agent completed"

run_managed_python \
  "$ROOT/runner/generate_patch.py" \
  --original "$ORIGINAL_CASE" \
  --modified "$WORKSPACE/work/repo" \
  --output "$RUN_DIR/repair.diff" \
  --allowed-prefix input/ \
  >"$INTERNAL_DIR/changed-paths.txt"
[[ -s "$RUN_DIR/repair.diff" ]] || fail "Canonical repair.diff is empty."
ok "Canonical patch generated"

while IFS= read -r relative; do
  [[ -n "$relative" ]] || continue
  original="$ORIGINAL_CASE/$relative"
  repaired="$WORKSPACE/work/repo/$relative"
  if [[ -f "$original" ]]; then
    mkdir -p "$RUN_DIR/evidence/original/$(dirname "$relative")"
    cp "$original" "$RUN_DIR/evidence/original/$relative"
  fi
  if [[ -f "$repaired" ]]; then
    mkdir -p "$RUN_DIR/evidence/repaired/$(dirname "$relative")"
    cp "$repaired" "$RUN_DIR/evidence/repaired/$relative"
  fi
done <"$INTERNAL_DIR/changed-paths.txt"

set +e
"$ROOT/runner/build-case-docker" \
  --input "$ORIGINAL_CASE" \
  --patch "$RUN_DIR/repair.diff" \
  --output "$FINAL_RESULT" \
  >"$INTERNAL_DIR/final-validator.console.log" 2>&1
final_exit=$?
set -e

[[ -f "$FINAL_RESULT/build-result.json" ]] || {
  cat "$INTERNAL_DIR/final-validator.console.log" >&2
  fail "Final Validator result is missing."
}
final_status="$(json_field "$FINAL_RESULT/build-result.json" status)"
if [[ $final_exit -ne 0 || "$final_status" != "succeeded" ]]; then
  cp "$FINAL_RESULT/build-result.json" "$RUN_DIR/build-result.json"
  cp "$FINAL_RESULT/build.log" "$RUN_DIR/build.log"
  tail -n 80 "$FINAL_RESULT/build.log" >&2 || true
  fail "Final validation did not succeed; status=$final_status exit=$final_exit."
fi

cp "$FINAL_RESULT/build-result.json" "$RUN_DIR/build-result.json"
cp "$FINAL_RESULT/build.log" "$RUN_DIR/build.log"
mkdir -p "$RUN_DIR/artifacts"
cp -a "$FINAL_RESULT/artifacts/." "$RUN_DIR/artifacts/"

case "$INTERNAL_DIR" in
  "$RUN_DIR/.internal")
    rm -rf "$INTERNAL_DIR"
    ;;
  *)
    fail "Refusing to clean an unexpected internal directory."
    ;;
esac
ok "Final validation succeeded"

printf '\n'
printf 'Result: %s\n' "$RUN_DIR/build-result.json"
printf 'Log:    %s\n' "$RUN_DIR/build.log"
printf 'Patch:  %s\n' "$RUN_DIR/repair.diff"
