#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
source "$ROOT/runner/result-json.sh"

usage='Usage: ./bb bootstrap NAME [--json] [--force]'
name=""
json_mode=false
force=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json)
      json_mode=true
      shift
      ;;
    --force)
      force=true
      shift
      ;;
    -*)
      fail "$usage" 2
      ;;
    *)
      [[ -z "$name" ]] || fail "$usage" 2
      name="$1"
      shift
      ;;
  esac
done

[[ -n "$name" ]] || fail "$usage" 2
if [[ ! "$name" =~ ^[a-z][a-z0-9-]{1,63}$ ]]; then
  if [[ "$json_mode" == true ]]; then
    bb_emit_result bootstrap failed 2 \
      "Agent workspace was not created." \
      "NAME must contain 2-64 lowercase letters, digits, or hyphens." \
      "" "" "" "" "./bb bootstrap my-agent --json"
    exit 2
  fi
  fail "NAME must contain 2-64 lowercase letters, digits, or hyphens" 2
fi

target="$ROOT/agents/$name"
if [[ -e "$target" && "$force" == false ]]; then
  if [[ "$json_mode" == true ]]; then
    bb_emit_result bootstrap failed 4 \
      "Agent workspace was not created." \
      "agents/$name already exists; choose another name or explicitly use --force." \
      "agent_workspace" "agents/$name" "" "" \
      "./bb ready --agent ./agents/$name --json"
    exit 4
  fi
  fail "Agent already exists: agents/$name. Choose another name or use --force." 4
fi

set +e
if [[ "$json_mode" == true ]]; then
  "$ROOT/runner/doctor.sh" --quiet 1>&2
else
  "$ROOT/runner/doctor.sh" --quiet
fi
doctor_exit=$?
set -e
if [[ $doctor_exit -ne 0 ]]; then
  if [[ "$json_mode" == true ]]; then
    bb_emit_result bootstrap failed 3 \
      "Local environment checks failed." \
      "Fix the Docker or image error printed on stderr, then run bootstrap again." \
      "" "" "" "" "./bb doctor"
    exit 3
  fi
  exit "$doctor_exit"
fi

if [[ -e "$target" && "$force" == true ]]; then
  rm -rf -- "$target"
fi

set +e
if [[ "$json_mode" == true ]]; then
  "$ROOT/runner/init-agent.sh" "$name" 1>&2
else
  "$ROOT/runner/init-agent.sh" "$name"
fi
init_exit=$?
set -e
if [[ $init_exit -ne 0 ]]; then
  if [[ "$json_mode" == true ]]; then
    bb_emit_result bootstrap failed 4 \
      "Agent workspace was not created." \
      "The Agent template could not be initialized; inspect stderr for details." \
      "" "" "" "" "./bb init $name"
    exit 4
  fi
  exit "$init_exit"
fi

if [[ "$json_mode" == true ]]; then
  bb_emit_result bootstrap succeeded 0 \
    "Agent workspace is ready." "" \
    "agent_workspace" "agents/$name" "" "" \
    "./bb ready --agent ./agents/$name --json"
else
  printf '\n'
  ok "Coding-agent workspace is ready"
  info "Read:  AGENTS.md"
  info "Edit:  agents/$name/src/"
  info "Ready: ./bb ready --agent ./agents/$name"
fi
