#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Build-Bench Starter Kit

Usage:
  ./bb doctor    Check the local Git and Docker environment
  ./bb demo      Run the official failure-to-success example
  ./bb bootstrap Prepare an editable Agent workspace for coding assistants
  ./bb init NAME Create an editable managed-Python Agent
  ./bb check     Validate an Agent submission directory
  ./bb test      Run an Agent against the local Example Cases
  ./bb package   Create a deterministic submission ZIP
  ./bb ready     Test, validate, and package one immutable Agent snapshot
  ./bb version   Show the Starter Kit version
  ./bb help      Show this help

Participant workflow:
  ./bb bootstrap my-agent
  # Edit agents/my-agent/src/
  ./bb ready --agent ./agents/my-agent

Granular workflow:
  ./bb init my-agent
  ./bb check --agent ./agents/my-agent
  ./bb test --agent ./agents/my-agent
  ./bb package --agent ./agents/my-agent
EOF
}

COMMAND="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$COMMAND" in
  doctor)
    exec "$ROOT/runner/doctor.sh" "$@"
    ;;
  demo)
    exec "$ROOT/runner/demo.sh" "$@"
    ;;
  bootstrap)
    exec "$ROOT/runner/bootstrap.sh" "$@"
    ;;
  init)
    exec "$ROOT/runner/init-agent.sh" "$@"
    ;;
  check)
    exec "$ROOT/runner/check-agent.sh" "$@"
    ;;
  test)
    exec "$ROOT/runner/test-agent.sh" "$@"
    ;;
  package)
    exec "$ROOT/runner/package-agent.sh" "$@"
    ;;
  ready)
    exec "$ROOT/runner/ready.sh" "$@"
    ;;
  version)
    cat "$ROOT/VERSION"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    printf 'Unknown command: %s\n\n' "$COMMAND" >&2
    usage >&2
    exit 2
    ;;
esac
