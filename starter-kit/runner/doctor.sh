#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

QUIET=false
if [[ "${1:-}" == "--quiet" ]]; then
  QUIET=true
  shift
fi
[[ $# -eq 0 ]] || fail "doctor accepts no additional arguments" 2

have_command git || fail "Git is not installed. Install Git and run ./bb doctor again."
have_command docker || fail "Docker is not installed. Install Docker and run ./bb doctor again."
docker info >/dev/null 2>&1 || fail "Docker daemon is not available. Start Docker and try again."

for image in \
  "$BB_AGENT_IMAGE" \
  "$BB_VALIDATOR_IMAGE" \
  "$BB_EXAMPLE_ASSETS_IMAGE" \
  "$BB_CLEANUP_IMAGE"
do
  if ! ensure_image "$image"; then
    fail "Image is unavailable: $image. Check network access or configure its BB_*_IMAGE variable."
  fi
done

available_kb="$(df -Pk "$ROOT" | awk 'NR == 2 {print $4}')"
if [[ "$available_kb" =~ ^[0-9]+$ ]] && (( available_kb < 3145728 )); then
  fail "Less than 3 GB of free disk space is available."
fi

if [[ "$QUIET" == false ]]; then
  ok "Git is available"
  ok "Docker is available"
  ok "Images are ready"
  info "Host architecture: $(uname -m)"
  info "Starter Kit: $(cat "$ROOT/VERSION")"
  info "Agent image: $BB_AGENT_IMAGE"
  info "Validator image: $BB_VALIDATOR_IMAGE"
  info "Example assets: $BB_EXAMPLE_ASSETS_IMAGE"
fi

