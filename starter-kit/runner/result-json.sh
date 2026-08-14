#!/usr/bin/env bash

# Small JSON emitter for commands that must also report failures before the
# managed Python image is available. Values are escaped as JSON strings.

bb_json_escape() {
  local value="${1-}"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '%s' "$value"
}

bb_json_string() {
  printf '"%s"' "$(bb_json_escape "${1-}")"
}

# Arguments:
# command status exit_code summary diagnostic artifact_type artifact_path
# artifact_sha256 artifact_bytes next_command
bb_emit_result() {
  local command="$1"
  local status="$2"
  local exit_code="$3"
  local summary="$4"
  local diagnostic="${5-}"
  local artifact_type="${6-}"
  local artifact_path="${7-}"
  local artifact_sha256="${8-}"
  local artifact_bytes="${9-}"
  local next_command="${10-}"

  printf '{\n'
  printf '  "schema_version": "0.1",\n'
  printf '  "command": %s,\n' "$(bb_json_string "$command")"
  printf '  "status": %s,\n' "$(bb_json_string "$status")"
  printf '  "exit_code": %s,\n' "$exit_code"
  printf '  "summary": %s,\n' "$(bb_json_string "$summary")"
  if [[ -n "$diagnostic" ]]; then
    printf '  "diagnostics": [{"message": %s}],\n' "$(bb_json_string "$diagnostic")"
  else
    printf '  "diagnostics": [],\n'
  fi
  if [[ -n "$artifact_path" ]]; then
    printf '  "artifacts": [{"type": %s, "path": %s' \
      "$(bb_json_string "$artifact_type")" \
      "$(bb_json_string "$artifact_path")"
    if [[ -n "$artifact_sha256" ]]; then
      printf ', "sha256": %s' "$(bb_json_string "$artifact_sha256")"
    fi
    if [[ -n "$artifact_bytes" ]]; then
      printf ', "bytes": %s' "$artifact_bytes"
    fi
    printf '}],\n'
  else
    printf '  "artifacts": [],\n'
  fi
  if [[ -n "$next_command" ]]; then
    printf '  "next_actions": [{"command": %s}]\n' "$(bb_json_string "$next_command")"
  else
    printf '  "next_actions": []\n'
  fi
  printf '}\n'
}
