#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/runner/common.sh"

RUN_DIR="$ROOT/runs/demo"
[[ -f "$RUN_DIR/build-result.json" ]] || fail "Run ./bb demo before verify-demo.sh."
[[ "$(json_field "$RUN_DIR/build-result.json" status)" == "succeeded" ]] \
  || fail "Final build status is not succeeded."
[[ "$(json_field "$RUN_DIR/initial-build-result.json" status)" == "failed" ]] \
  || fail "Initial build status is not failed."
[[ "$(json_field "$RUN_DIR/agent-result.json" status)" == "completed" ]] \
  || fail "Agent status is not completed."
[[ "$(json_field "$RUN_DIR/build-result.json" patch_applied)" == "true" ]] \
  || fail "Final Validator did not apply the patch."
[[ "$(json_field "$RUN_DIR/build-result.json" artifact_validation_passed)" == "true" ]] \
  || fail "Artifact validation did not pass."

artifact_count="$(find "$RUN_DIR/artifacts" -maxdepth 1 -type f | wc -l)"
[[ "$artifact_count" -eq 2 ]] || fail "Expected exactly two package artifacts."
find "$RUN_DIR/artifacts" -maxdepth 1 -type f -name '*.rpm' | grep -q .
find "$RUN_DIR/artifacts" -maxdepth 1 -type f -name '*.src.rpm' | grep -q .

grep -q '^diff --git a/input/' "$RUN_DIR/repair.diff"
if grep '^diff --git ' "$RUN_DIR/repair.diff" | grep -vq \
  '^diff --git a/input/.* b/input/'; then
  fail "repair.diff contains a path outside input/."
fi

grep -q 'BUILD-BENCH-DEMO-BROKEN' \
  "$RUN_DIR/evidence/original/input/buildbench-hello.spec"
grep -q 'BUILD-BENCH-DEMO-REPAIRED' \
  "$RUN_DIR/evidence/repaired/input/buildbench-hello.spec"
[[ ! -e "$RUN_DIR/.internal" ]] \
  || fail "Successful Demo retained its large internal workspace."

latest_archive="$(
  find "$ROOT/runs/archive" -mindepth 1 -maxdepth 1 -type d 2>/dev/null \
    | sort \
    | tail -1
)"
if [[ -n "$latest_archive" && -f "$latest_archive/repair.diff" ]]; then
  cmp "$RUN_DIR/repair.diff" "$latest_archive/repair.diff"
fi

set +e
"$ROOT/bb" unknown >/dev/null 2>&1
unknown_exit=$?
set -e
[[ "$unknown_exit" -eq 2 ]] || fail "Unknown bb command must exit with code 2."

ok "Milestone A demo outputs are valid"
info "Final status: succeeded"
info "Initial status: failed"
info "Artifacts: $artifact_count"
info "Patch: $RUN_DIR/repair.diff"
